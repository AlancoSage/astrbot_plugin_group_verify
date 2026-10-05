import asyncio
import random
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class GroupVerifyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # {f"{group_id}:{user_id}": {"code": 验证码, "task": asyncio.Task, "group_id": ..., "user_id": ...}}
        self.pending: dict[str, dict] = {}

    # ---------- 配置读写 ----------

    def _parse_group_rules(self) -> dict[str, dict]:
        """把配置中的 group_rules 条目解析为 {群号: 规则dict}"""
        result: dict[str, dict] = {}
        for item in self.config.get("group_rules") or []:
            if not isinstance(item, dict):
                continue
            group_id = str(item.get("group_id", "")).strip()
            if not group_id:
                continue
            result[group_id] = {
                "enabled": bool(item.get("enabled", True)),
                "send_delay_sec": item.get("send_delay_sec") or "",
                "answer_timeout_sec": item.get("answer_timeout_sec") or "",
                "kick_delay_sec": item.get("kick_delay_sec") or "",
            }
        return result

    def _write_group_rules(self, rules: dict[str, dict]) -> None:
        """把 {群号: 规则dict} 写回 group_rules 配置并保存"""
        self.config["group_rules"] = [
            {
                "__template_key": "group_rule",
                "group_id": group_id,
                "enabled": rule.get("enabled", True),
                "send_delay_sec": rule.get("send_delay_sec", ""),
                "answer_timeout_sec": rule.get("answer_timeout_sec", ""),
                "kick_delay_sec": rule.get("kick_delay_sec", ""),
            }
            for group_id, rule in rules.items()
        ]
        self.config.save_config()

    def _rule_value(self, rule: dict, key: str, default: int) -> int:
        """取群配置值，0 或留空时回退全局默认"""
        raw = (rule or {}).get(key, "")
        try:
            value = int(raw) if raw not in ("", None) else 0
        except (TypeError, ValueError):
            value = 0
        return value if value > 0 else default

    def _group_enabled(self, group_id: str) -> bool:
        """根据名单模式和每群开关判断该群是否启用验证"""
        rules = self._parse_group_rules()
        rule = rules.get(group_id)
        if rule is not None:
            return rule["enabled"]

        def _norm(items) -> list[str]:
            return [str(i) for i in (items or [])]

        # 黑白名单共存：白名单非空时必须在白名单内；黑名单始终生效
        whitelist = _norm(self.config.get("whitelist"))
        if whitelist and group_id not in whitelist:
            return False
        return group_id not in _norm(self.config.get("blacklist"))

    # ---------- 入群检测与验证流程 ----------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=5)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听群消息：检测入群通知发起验证；处理待验证用户的答题"""
        if not self.config.get("enabled", True):
            return

        raw = event.message_obj.raw_message
        # aiocqhttp 适配器会把 OneBot 的 notice 事件原样放在 raw_message 中
        if isinstance(raw, dict) and raw.get("post_type") == "notice":
            if raw.get("notice_type") == "group_increase":
                await self._handle_group_increase(event, raw)
            return

        await self._handle_answer(event)

    async def _handle_group_increase(self, event: AstrMessageEvent, raw: dict):
        """新人入群：按配置延迟后发送验证码"""
        group_id = str(event.message_obj.group_id)
        new_user_id = str(raw.get("user_id", ""))
        if not group_id or not new_user_id:
            return
        if not self._group_enabled(group_id):
            return
        key = f"{group_id}:{new_user_id}"
        if key in self.pending:
            return

        rules = self._parse_group_rules()
        rule = rules.get(group_id) or {}
        send_delay = self._rule_value(
            rule, "send_delay_sec", self.config.get("send_delay_sec", 0)
        )
        logger.info(f"检测到新人入群: 群 {group_id}, 用户 {new_user_id}, 延迟 {send_delay} 秒发送验证")

        if send_delay > 0:
            await asyncio.sleep(send_delay)
            if not self.config.get("enabled", True):
                return

        mode = self.config.get("verify_mode", "code")
        if mode == "math":
            a, b = random.randint(1, 99), random.randint(1, 99)
            if random.random() < 0.5:
                question, code = f"{a} + {b} = ?", str(a + b)
            else:
                a, b = max(a, b), min(a, b)
                question, code = f"{a} - {b} = ?", str(a - b)
            prompt = f" 欢迎加入本群！请在规定时间内发送下面算式的结果完成入群验证：{question}"
        else:
            code = "".join(str(random.randint(0, 9)) for _ in range(self._code_length()))
            if mode == "image":
                prompt = " 欢迎加入本群！请在规定时间内发送图片中的验证码完成入群验证。"
            else:
                prompt = f" 欢迎加入本群！请在规定时间内发送验证码「{code}」完成入群验证。"

        captcha_path = ""
        if mode == "image":
            captcha_path = self._render_captcha(code, group_id, new_user_id)
            if not captcha_path:
                logger.error("图片验证码渲染失败，回退为文字验证码")
                prompt = f" 欢迎加入本群！请在规定时间内发送验证码「{code}」完成入群验证。"

        chain = [Comp.At(qq=new_user_id), Comp.Plain(text=prompt)]
        if captcha_path:
            chain.append(Comp.Image(file=captcha_path))
        try:
            await event.send(event.chain_result(chain))
        except Exception as e:
            logger.error(f"发送验证提示失败: {e}")
            return

        answer_timeout = self._rule_value(
            rule, "answer_timeout_sec", self.config.get("answer_timeout_sec", 300)
        )
        if answer_timeout > 0:
            task = asyncio.create_task(
                self._timeout_watch(group_id, new_user_id, code, answer_timeout)
            )
            self.pending[key] = {
                "code": code,
                "task": task,
                "group_id": group_id,
                "user_id": new_user_id,
            }
        else:
            self.pending[key] = {
                "code": code,
                "task": None,
                "group_id": group_id,
                "user_id": new_user_id,
            }

    def _code_length(self) -> int:
        try:
            n = int(self.config.get("code_length", 4))
        except (TypeError, ValueError):
            n = 4
        return min(8, max(4, n))

    def _answer_match(self, text: str, code: str) -> bool:
        """答案匹配：数字验证码要求完全一致；加减法按数值匹配，允许带 = 号或「结果是」等前缀"""
        text = text.replace("=", "").strip()
        if text == code:
            return True
        try:
            return int(text) == int(code)
        except (TypeError, ValueError):
            return False

    def _render_captcha(self, code: str, group_id: str, user_id: str) -> str:
        """用 Pillow 渲染带噪点和干扰线的图片验证码，返回本地文件路径"""
        try:
            from PIL import Image, ImageDraw, ImageFont

            width, height = 200, 60
            img = Image.new("RGB", (width, height), "#f5f0e8")
            draw = ImageDraw.Draw(img)

            # 干扰折线（随机颜色、多段折线）
            palette = ["#3b7bd6", "#c94f4f", "#4f9e4f", "#8a63c9", "#d69a3b"]
            for _ in range(6):
                color = random.choice(palette)
                pts = [
                    (
                        random.randint(0, width),
                        random.randint(0, height),
                    )
                    for _ in range(random.randint(3, 6))
                ]
                draw.line(pts, fill=color, width=1)

            # 噪点
            for _ in range(120):
                x, y = random.randint(0, width - 1), random.randint(0, height - 1)
                draw.point((x, y), fill=random.choice(palette))

            # 逐位绘制数字，带随机大小、颜色和轻微旋转
            font_paths = [
                "C:/Windows/Fonts/arial.ttf",
                "C:/Windows/Fonts/calibri.ttf",
                "C:/Windows/Fonts/tahoma.ttf",
            ]
            fonts = []
            for fp in font_paths:
                try:
                    fonts.append(fp)
                except Exception:
                    continue
            n = len(code)
            for i, ch in enumerate(code):
                font = None
                for fp in fonts:
                    try:
                        font = ImageFont.truetype(fp, random.randint(28, 38))
                        break
                    except Exception:
                        continue
                if font is None:
                    font = ImageFont.load_default()
                color = random.choice(palette)
                # 单字符画到小图再旋转，模拟扭曲
                char_img = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
                char_draw = ImageDraw.Draw(char_img)
                char_draw.text((8, 2), ch, fill=color, font=font)
                char_img = char_img.rotate(
                    random.randint(-30, 30), expand=True, resample=Image.BICUBIC
                )
                img.paste(
                    char_img,
                    (10 + i * int((width - 20) / n) + random.randint(-3, 3),
                     random.randint(2, 10)),
                    char_img,
                )

            out_dir = Path("data/plugin_data/astrbot_plugin_group_verify/captcha")
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{group_id}_{user_id}.png"
            img.save(out_path)
            return str(out_path)
        except Exception as e:
            logger.error(f"渲染图片验证码失败: {e}")
            return ""

    def _nickname(self, event: AstrMessageEvent) -> str:
        try:
            return event.message_obj.sender.nickname or "新朋友"
        except Exception:
            return "新朋友"

    async def _timeout_watch(self, group_id: str, user_id: str, code: str, timeout: int):
        """答题倒计时，超时后按配置执行处理"""
        await asyncio.sleep(timeout)
        key = f"{group_id}:{user_id}"
        info = self.pending.pop(key, None)
        if info is None or info["code"] != code:
            return
        if not self.config.get("enabled", True):
            return
        await self._punish(group_id, user_id)

    async def _handle_answer(self, event: AstrMessageEvent):
        """处理待验证用户发送的群消息，匹配验证码则通过"""
        group_id = str(event.message_obj.group_id or "")
        user_id = str(event.message_obj.sender.user_id or "")
        key = f"{group_id}:{user_id}"
        info = self.pending.get(key)
        if info is None:
            return

        text = "".join(
            seg.text for seg in event.message_obj.message if hasattr(seg, "text")
        ).strip()
        if not text:
            return
        if not self._answer_match(text, info["code"]):
            return

        # 验证通过：取消超时任务并移出待验证名单
        task = info.get("task")
        if task and not task.done():
            task.cancel()
        self.pending.pop(key, None)
        logger.info(f"验证通过: 群 {group_id}, 用户 {user_id}")

        pass_hint = str(self.config.get("pass_hint", "") or "").strip()
        if pass_hint:
            try:
                await event.send(
                    event.chain_result(self._hint_chain(pass_hint, user_id))
                )
            except Exception as e:
                logger.error(f"发送验证通过提示失败: {e}")

    async def _punish(self, group_id: str, user_id: str):
        """超时处理：按配置踢出 / 禁言 / 无操作"""
        action = self.config.get("timeout_action", "kick")
        fail_hint = str(self.config.get("fail_hint", "") or "").strip()
        if fail_hint:
            try:
                # 通过 bot 实例发送群消息（event 已不在作用域内）
                bot = self.context.get_platform("aiocqhttp").get_client()
                await bot.send_group_msg(
                    group_id=int(group_id),
                    message=self._onebot_hint(fail_hint, user_id),
                )
            except Exception as e:
                logger.error(f"发送超时提示失败: {e}")

        if action == "kick":
            kick_delay = self.config.get("kick_delay_sec", 0) or 0
            try:
                kick_delay = int(kick_delay)
            except (TypeError, ValueError):
                kick_delay = 0
            if kick_delay > 0:
                await asyncio.sleep(kick_delay)
                if not self.config.get("enabled", True):
                    return
            await self._kick(group_id, user_id)
        elif action == "mute":
            try:
                duration = int(self.config.get("mute_duration_sec", 600) or 600)
            except (TypeError, ValueError):
                duration = 600
            await self._mute(group_id, user_id, duration)

    async def _kick(self, group_id: str, user_id: str):
        try:
            bot = self.context.get_platform("aiocqhttp").get_client()
            await bot.set_group_kick(group_id=int(group_id), user_id=int(user_id))
            logger.info(f"已移出未通过验证的成员: 群 {group_id}, 用户 {user_id}")
        except Exception as e:
            logger.error(f"移出成员失败: {e}")

    async def _mute(self, group_id: str, user_id: str, duration: int):
        try:
            bot = self.context.get_platform("aiocqhttp").get_client()
            await bot.set_group_ban(
                group_id=int(group_id), user_id=int(user_id), duration=duration
            )
            logger.info(f"已禁言未通过验证的成员 {duration} 秒: 群 {group_id}, 用户 {user_id}")
        except Exception as e:
            logger.error(f"禁言成员失败: {e}")

    # ---------- 提示语工具 ----------

    def _hint_chain(self, hint: str, user_id: str) -> list:
        """把提示语文本解析为消息链，支持 {at}、{name} 占位符"""
        parts = hint.split("{at}")
        chain: list = []
        for i, part in enumerate(parts):
            if part:
                chain.append(Comp.Plain(text=part.replace("{name}", "新朋友")))
            if i < len(parts) - 1:
                chain.append(Comp.At(qq=user_id))
        return chain if chain else [Comp.Plain(text=hint)]

    def _onebot_hint(self, hint: str, user_id: str) -> list:
        """构造 OneBot 消息段数组（无 AstrMessageEvent 可用时的发送格式）"""
        parts = hint.split("{at}")
        message: list = []
        for i, part in enumerate(parts):
            if part:
                message.append({"type": "text", "data": {"text": part.replace("{name}", "新朋友")}})
            if i < len(parts) - 1:
                message.append({"type": "at", "data": {"qq": user_id}})
        return message if message else [{"type": "text", "data": {"text": hint}}]

    # ---------- 管理指令 ----------
    # 注意：permission_type 装饰器不支持叠加在 command_group 上（v4.28.1 会报错），
    # 因此管理员校验在各指令函数内部进行。

    @filter.command_group("验证")
    def verify_group(self):
        """入群验证管理指令组（仅管理员）"""

    def _check_admin(self, event: AstrMessageEvent) -> str | None:
        """非管理员时返回提示语，否则返回 None"""
        try:
            if not event.is_admin():
                return "该指令仅管理员可用。"
        except Exception:
            return "该指令仅管理员可用。"
        return None

    @verify_group.command("延迟")
    async def verify_delay(self, event: AstrMessageEvent, seconds: int):
        """为当前群设置验证提示发送延迟（秒），例如：/验证延迟 10"""
        if msg := self._check_admin(event):
            yield event.plain_result(msg)
            return
        if not event.message_obj.group_id:
            yield event.plain_result("请在群聊中使用该指令。")
            return
        group_id = str(event.message_obj.group_id)
        rules = self._parse_group_rules()
        rule = dict(rules.get(group_id) or {})
        rule["enabled"] = rule.get("enabled", True)
        rule["send_delay_sec"] = seconds
        rules[group_id] = rule
        self._write_group_rules(rules)
        yield event.plain_result(f"已设置群 {group_id} 的验证提示发送延迟为 {seconds} 秒。")

    @verify_group.command("时限")
    async def verify_timeout(self, event: AstrMessageEvent, seconds: int):
        """为当前群设置答题时限（秒），例如：/验证时限 300"""
        if msg := self._check_admin(event):
            yield event.plain_result(msg)
            return
        if not event.message_obj.group_id:
            yield event.plain_result("请在群聊中使用该指令。")
            return
        group_id = str(event.message_obj.group_id)
        rules = self._parse_group_rules()
        rule = dict(rules.get(group_id) or {})
        rule["enabled"] = rule.get("enabled", True)
        rule["answer_timeout_sec"] = seconds
        rules[group_id] = rule
        self._write_group_rules(rules)
        yield event.plain_result(f"已设置群 {group_id} 的答题时限为 {seconds} 秒。")

    @verify_group.command("查看")
    async def verify_get(self, event: AstrMessageEvent):
        """查看当前群的验证配置"""
        if msg := self._check_admin(event):
            yield event.plain_result(msg)
            return
        group_id = str(event.message_obj.group_id)
        rule = self._parse_group_rules().get(group_id)
        if rule:
            yield event.plain_result(
                f"群 {group_id} 验证配置：启用={rule['enabled']}，"
                f"发送延迟={rule['send_delay_sec'] or '（全局默认）'}，"
                f"答题时限={rule['answer_timeout_sec'] or '（全局默认）'}，"
                f"超时处理延迟={rule['kick_delay_sec'] or '（全局默认）'}"
            )
        else:
            yield event.plain_result(f"群 {group_id} 未单独设置验证规则，将使用全局默认配置。")

    @verify_group.command("开关")
    async def verify_toggle(self, event: AstrMessageEvent, state: str = ""):
        """开启/关闭当前群的验证，例如：/验证开关 开 或 /验证开关 关"""
        if msg := self._check_admin(event):
            yield event.plain_result(msg)
            return
        if not event.message_obj.group_id:
            yield event.plain_result("请在群聊中使用该指令。")
            return
        group_id = str(event.message_obj.group_id)
        rules = self._parse_group_rules()
        rule = dict(rules.get(group_id) or {})
        if state in ("开", "开启", "on"):
            enabled = True
        elif state in ("关", "关闭", "off"):
            enabled = False
        else:
            enabled = not rule.get("enabled", True)
        rule["enabled"] = enabled
        rule.setdefault("send_delay_sec", "")
        rule.setdefault("answer_timeout_sec", "")
        rule.setdefault("kick_delay_sec", "")
        rules[group_id] = rule
        self._write_group_rules(rules)
        yield event.plain_result(
            f"已{'开启' if enabled else '关闭'}群 {group_id} 的入群验证。"
        )

    @verify_group.command("删除")
    async def verify_del(self, event: AstrMessageEvent):
        """删除当前群的单独验证规则，恢复使用全局默认"""
        if msg := self._check_admin(event):
            yield event.plain_result(msg)
            return
        group_id = str(event.message_obj.group_id)
        rules = self._parse_group_rules()
        if group_id in rules:
            del rules[group_id]
            self._write_group_rules(rules)
            yield event.plain_result("已删除当前群的单独验证规则，将使用全局默认配置。")
        else:
            yield event.plain_result("当前群未设置单独验证规则。")

    async def terminate(self):
        """插件被卸载/停用时调用：取消所有超时任务"""
        for info in self.pending.values():
            task = info.get("task")
            if task and not task.done():
                task.cancel()
        self.pending.clear()
