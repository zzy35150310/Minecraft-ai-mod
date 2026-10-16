"""spec 的校验、默认值填充，以及「需求 → spec」的 LLM 翻译。

spec 是需求和模板之间的唯一契约。它必须窄——只认白名单里的内容类型，
否则会退化成"什么都支持但什么都不 work"。

本模块不直接发 HTTP：spec_from_request 接收一个 chat(system, user) 可调用对象，
把网络调用留给 mcgen_llm。这样校验与重试逻辑可以脱网测试——
而重试逻辑恰恰是最容易写错、又最不容易被发现写错的部分。
"""

from __future__ import annotations

import json
import re

# modid 会被派生成 Java 包名和类名，所以只收 [a-z0-9_]。
# Minecraft 的命名空间其实还允许 '-' 和 '.'，但那会让 com.example.my-mod 这种
# 包名非法，所以这里比原版更严——是有意的取舍，不是疏漏。
MODID_RE = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
PACKAGE_RE = re.compile(r'^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$')

# 内容类型白名单。加类型必须同时补 mcgen_render 里的注册模板。
CONTENT_TYPES = {'item', 'block', 'tool', 'armor', 'food', 'recipe', 'drop', 'event_hook'}

# 目前只有 item 这条链路是通的，其余在 P5 补
IMPLEMENTED_TYPES = {'item'}


def normalize_modid(text: str) -> str:
    """把任意文本压成合法 modid。"""
    s = re.sub(r'[^a-z0-9]+', '_', (text or '').lower()).strip('_')
    if not s or not s[0].isalpha():
        s = 'mod_' + s
    return s[:64]


def validate(spec: dict) -> list:
    """返回问题列表，空列表表示通过。"""
    problems = []

    modid = spec.get('modid') or ''
    if not MODID_RE.match(modid):
        problems.append('modid 不合法（要求小写字母开头、仅含小写字母数字下划线）：%r' % modid)

    package = spec.get('package') or ''
    if not PACKAGE_RE.match(package):
        problems.append('package 不合法（要求形如 com.example.mymod）：%r' % package)

    content = spec.get('content')
    if content is None:
        problems.append('缺少 content 字段')
    elif not isinstance(content, list):
        problems.append('content 必须是列表')
    else:
        seen = set()
        for idx, item in enumerate(content):
            if not isinstance(item, dict):
                problems.append('content[%d] 不是对象' % idx)
                continue
            ctype = item.get('type')
            if ctype not in CONTENT_TYPES:
                problems.append('content[%d].type 不在白名单内：%r（允许：%s）'
                                % (idx, ctype, ', '.join(sorted(CONTENT_TYPES))))
            cid = item.get('id')
            if not cid or not MODID_RE.match(str(cid)):
                problems.append('content[%d].id 不合法：%r' % (idx, cid))
            elif cid in seen:
                problems.append('content[%d].id 重复：%r' % (idx, cid))
            else:
                seen.add(cid)

    return problems


def unsupported(spec: dict) -> list:
    """挑出类型合法但还没实现的条目，用于给出诚实的提示而不是静默丢弃。"""
    out = []
    for item in spec.get('content') or []:
        if isinstance(item, dict) and item.get('type') not in IMPLEMENTED_TYPES:
            out.append(item.get('type'))
    return out


def apply_defaults(spec: dict) -> dict:
    """补齐可推导的字段，返回新 dict（不改原对象）。"""
    out = dict(spec)
    out['modid'] = normalize_modid(out.get('modid') or out.get('name') or 'mymod')
    out['package'] = out.get('package') or ('com.example.' + out['modid'])
    out.setdefault('name', out['modid'])
    out.setdefault('version', '1.0.0')
    out.setdefault('description', '')
    out.setdefault('authors', [])
    out.setdefault('content', [])
    for item in out['content']:
        if isinstance(item, dict):
            item.setdefault('id', normalize_modid(item.get('name') or 'thing'))
    return out


# ---------- 需求 → spec ----------

SYSTEM_PROMPT = """你是一个 Minecraft Fabric 模组的需求分析器。

你的唯一任务：把用户的中文需求翻译成一份 JSON 规格（spec）。
**不要写任何 Java 代码**，代码由后续环节用模板和真实 API 签名生成。

只输出一个 JSON 对象，不要 markdown 围栏，不要任何解释文字。

字段：
  modid       必填。小写字母开头，只含小写字母/数字/下划线，不超过 64 字符
  name        模组显示名
  description 一句话描述
  content     内容条目数组，至少一项

content 每一项：
  type  只能是：item / block / tool / armor / food / recipe / drop / event_hook
  id    小写字母开头的小写标识符，同一份 spec 内不可重复
  name  显示名（中文可以）
  其余按类型给：
    item        max_stack(整数) max_damage(整数) behavior(行为描述)
    block       hardness(小数)
    tool        material(diamond/iron/...) kind(sword/pickaxe/...)
    armor       material slot(helmet/chest/legs/boots)
    food        nutrition(整数) saturation(小数)
    recipe      pattern(字符串数组) keys(对象)
    event_hook  event(事件名) behavior(行为描述)

规则：
1. 贴图直接复用原版素材，不要发明新贴图。需要时加 texture 字段，
   值必须是原版资源路径，例如 "minecraft:item/diamond_sword"、
   "minecraft:block/stone"。
2. behavior 是唯一会交给代码生成模型的部分，写具体些：
   触发条件 + 具体效果。例如"用右键击中生物时，在目标位置生成威力 2.0 的爆炸"。
3. 用户没提的参数用合理默认值填上，不要留空。
4. 只输出 JSON。

示例输出：
{"modid":"boomsword","name":"爆炸剑","description":"会爆炸的钻石剑",
 "content":[{"type":"item","id":"boom_sword","name":"爆炸钻石剑","max_stack":1,
   "max_damage":1561,"texture":"minecraft:item/diamond_sword",
   "behavior":"用右键击中生物时，在目标位置生成威力 2.0 的爆炸，不破坏方块"}]}"""


def build_spec_prompt(req: str) -> str:
    """把用户需求包成一次请求的 user 部分。"""
    return '需求：%s\n\n请输出 spec JSON。' % req


def extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON 对象。

    模型很少老老实实只输出 JSON：常见的是套一层 ```json 围栏，
    或者前后加一句"好的，这是规格："。都要容忍——为空这点小事重试一次不值得。
    """
    s = (text or '').strip()
    fence = re.search(r'```(?:json)?\s*(.*?)```', s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # 退一步：抠第一个 { 到最后一个 }
    start = s.find('{')
    end = s.rfind('}')
    if start >= 0 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError('模型返回的不是合法 JSON：%s' % exc)
    raise ValueError('模型返回里找不到 JSON 对象')


def spec_from_request(chat, req: str, max_retries: int = 2) -> dict:
    """需求文本 → 校验过的 spec。

    chat 是 chat(system, user) -> str 的可调用对象（真的调用时传
    mcgen_llm.llm_chat 的偏应用）。传进来而不是内部直接调，
    是为了让这套「生成→解析→校验→回喂」的重试逻辑可以脱网测试。

    校验不过就把问题回喂让模型改，而不是自己猜着补——
    猜错了会变成编译期才爆的错，那时更难定位。
    """
    feedback = ''
    last_problem = '未知错误'

    for _ in range(max_retries + 1):
        user = build_spec_prompt(req)
        if feedback:
            user += ('\n\n你上一次的输出不能通过校验，问题如下，'
                     '请修正后重新输出完整 JSON：\n' + feedback)
        raw = chat(SYSTEM_PROMPT, user)
        try:
            data = extract_json(raw)
        except ValueError as exc:
            last_problem = str(exc)
            feedback = last_problem
            continue

        if not isinstance(data, dict):
            last_problem = '返回的 JSON 顶层不是对象'
            feedback = last_problem
            continue

        spec = apply_defaults(data)
        problems = validate(spec)
        if not problems:
            return spec
        last_problem = '\n'.join('- ' + p for p in problems)
        feedback = last_problem

    raise ValueError('生成 spec 连续 %d 次没通过校验。最后一次的问题：\n%s'
                     % (max_retries + 1, last_problem))
