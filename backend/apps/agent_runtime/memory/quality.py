"""Deterministic admission rules for long-term memory candidates.

These rules deliberately run before OpenViking's LLM extraction.  They remove
obvious conversational noise and narrow the schemas that the native extractor
is allowed to consider; OpenViking remains responsible for semantic extraction,
deduplication, and merging.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


PROFILE_TYPES = frozenset({'profile'})
PREFERENCE_TYPES = frozenset({'profile', 'preferences'})
ENTITY_TYPES = frozenset({'entities'})
EVENT_TYPES = frozenset({'events'})
GENERIC_EXPLICIT_TYPES = frozenset({
    'profile',
    'preferences',
    'entities',
    'events',
    'cases',
})

MIN_TASK_RESPONSE_CHARS = 20
MIN_TOOL_RESULT_CHARS = 12

_TRIVIAL_TURNS = frozenset({
    '你好', '您好', '嗨', '哈喽', '在吗', '谢谢', '感谢', '好的', '好', '收到',
    '知道了', '明白了', '再见', '测试', '试一下', 'hi', 'hello', 'hey',
    'thanks', 'thankyou', 'ok', 'okay', 'gotit', 'bye', 'test', 'ping',
})
_EXPLICIT_MEMORY_RE = re.compile(
    r'(?:请|帮我)?(?:记住|记一下|保存到记忆|别忘了)|'
    r'(?:以后|今后).{0,12}(?:请|要|称呼|叫|喊|使用)|'
    r'\b(?:remember|save this|keep in mind|don[\'’]?t forget)\b',
    re.IGNORECASE,
)
_PREFERENCE_RE = re.compile(
    r'(?:称呼|叫我|喊我|喜欢|偏好|习惯|不喜欢|讨厌|希望你|回复时|回答时|'
    r'以后.{0,10}(?:用|不要|请))|'
    r'\b(?:call me|i prefer|i like|i dislike|my preference|please use)\b',
    re.IGNORECASE,
)
_PROFILE_RE = re.compile(
    r'(?:我叫|我的名字|我是.{0,20}(?:人|工程师|经理|总监|CEO|负责人|学生|老师)|'
    r'我的(?:职位|职业|生日|邮箱|电话|地址|公司|部门)|我在.{0,30}(?:工作|任职))|'
    r'\b(?:my name is|i am an?|i work (?:at|for)|my (?:job|role|email|phone))\b',
    re.IGNORECASE,
)
_ENTITY_RE = re.compile(
    r'(?:公司|项目|产品|客户|同事|老板|团队|部门|联系人|负责人).{0,30}'
    r'(?:叫|是|为|由|位于|负责|属于)|'
    r'\b(?:company|project|product|customer|colleague|team|contact).{0,30}'
    r'(?:is|named|belongs|responsible)\b',
    re.IGNORECASE,
)
_EVENT_RE = re.compile(
    r'(?:决定|计划|已完成|完成了|已上线|发布了|签署|开会|会议|截止|发生|'
    r'开始了|结束了|取消了|延期|通过了|拒绝了)|'
    r'\b(?:decided|planned|completed|launched|released|signed|deadline|'
    r'cancelled|postponed|approved|rejected)\b',
    re.IGNORECASE,
)
_TASK_RE = re.compile(
    r'(?:帮我|请你|请帮|需要你|给我).{0,12}'
    r'(?:起草|撰写|写|生成|制作|创建|分析|整理|总结|翻译|计算|查询|搜索|'
    r'调研|设计|规划|修改|修复|处理|执行|部署|配置)|'
    r'(?:^|[，。；、\s])(?:起草|撰写|生成|制作|创建|分析|整理|总结|翻译|'
    r'计算|查询|搜索|调研|设计|规划|修改|修复|处理|部署|配置)|'
    r'\b(?:draft|write|generate|create|analy[sz]e|summari[sz]e|translate|'
    r'calculate|search|research|design|plan|modify|fix|deploy|configure)\b',
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(
    r'(?:[?？]\s*$)|(?:^|\s)(?:什么|为什么|为何|如何|怎么|是否|能否|请问|谁|哪里|哪一个)|'
    r'^\s*(?:what|why|how|is|are|can|could|who|where|which)\b',
    re.IGNORECASE,
)
_SMOKE_TEST_RE = re.compile(
    r'(?:测试工具|工具调用测试|冒烟测试|测试(?:一下)?(?:python|调用)|'
    r'原样返回\s*stdout|getpass\.getuser|hello\s*world)|'
    r'\b(?:tool\s*(?:call)?\s*test|smoke\s*test)\b',
    re.IGNORECASE,
)
_NUMBERED_STEP_RE = re.compile(
    r'(?:^|\n)\s*(?:\d{1,2}[.)、]|[一二三四五六七八九十]+[、.)])\s*',
)


@dataclass(frozen=True)
class MemoryCandidateAssessment:
    eligible: bool
    memory_types: frozenset[str]
    reason: str


def assess_memory_candidate(
    user_content: object,
    assistant_content: object,
    *,
    has_successful_tool: bool = False,
) -> MemoryCandidateAssessment:
    """Classify an interaction before invoking native memory extraction."""
    user_text = str(user_content or '').strip()
    assistant_text = str(assistant_content or '').strip()
    if not user_text or not assistant_text:
        return MemoryCandidateAssessment(False, frozenset(), 'empty_turn')

    explicit_memory = bool(_EXPLICIT_MEMORY_RE.search(user_text))
    if is_trivial_turn(user_text) and not explicit_memory:
        return MemoryCandidateAssessment(False, frozenset(), 'trivial_turn')

    memory_types: set[str] = set()
    if _PREFERENCE_RE.search(user_text):
        memory_types.update(PREFERENCE_TYPES)
    if _PROFILE_RE.search(user_text):
        memory_types.update(PROFILE_TYPES)

    is_question = bool(_QUESTION_RE.search(user_text))
    if not is_question and _ENTITY_RE.search(user_text):
        memory_types.update(ENTITY_TYPES)
    if not is_question and _EVENT_RE.search(user_text):
        memory_types.update(EVENT_TYPES)

    smoke_test = is_smoke_test(user_text)
    task_candidate = is_task_candidate(user_text)
    meaningful_response = normalized_length(assistant_text) >= MIN_TASK_RESPONSE_CHARS
    if task_candidate and not smoke_test and (
        meaningful_response or has_successful_tool or is_multistep_task(user_text)
    ):
        memory_types.add('cases')

    if explicit_memory and not memory_types:
        memory_types.update(GENERIC_EXPLICIT_TYPES)

    if not memory_types:
        reason = 'tool_smoke_test' if smoke_test else 'no_durable_signal'
        return MemoryCandidateAssessment(False, frozenset(), reason)
    reason = 'explicit_memory' if explicit_memory else (
        'meaningful_task' if 'cases' in memory_types else 'durable_user_information'
    )
    return MemoryCandidateAssessment(True, frozenset(memory_types), reason)


def has_meaningful_successful_tool_result(tool_results: object) -> bool:
    for result in tool_results or ():
        if not isinstance(result, dict) or not bool(result.get('success')):
            continue
        if result.get('artifact_refs'):
            return True
        if normalized_length(result.get('content')) >= MIN_TOOL_RESULT_CHARS:
            return True
    return False


def is_multistep_task(value: object) -> bool:
    text = str(value or '')
    if len(_NUMBERED_STEP_RE.findall(text)) >= 2:
        return True
    return (
        '先' in text
        and ('再' in text or '然后' in text)
        and ('最后' in text or '最终' in text)
    )


def is_smoke_test(value: object) -> bool:
    return bool(_SMOKE_TEST_RE.search(str(value or '')))


def is_task_candidate(value: object) -> bool:
    text = str(value or '').strip()
    return bool(_TASK_RE.search(text)) or is_multistep_task(text)


def is_trivial_turn(value: object) -> bool:
    return _normalize(value) in _TRIVIAL_TURNS


def normalized_length(value: object) -> int:
    return len(_normalize(value))


def _normalize(value: object) -> str:
    return re.sub(r'[^\w\u4e00-\u9fff]+', '', str(value or '').casefold())
