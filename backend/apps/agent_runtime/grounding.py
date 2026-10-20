"""Hard grounding policy for company-internal factual answers."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any


GROUNDING_RETRY_INSTRUCTION = (
    "上一版事实性回答因缺少本次运行的有效内部资料证据而被平台拦截。"
    "不要根据历史消息、长期记忆或常识作答。请先使用 workspace.locate 查找相关文件，"
    "再对具体文件调用 workspace.read；只有读取到与问题目标相关的正文后才能回答，"
    "并在回答中标明 logical_path 来源。"
)

GROUNDING_REFUSAL = (
    "未成功读取到与本次问题相关的授权内部资料，因此无法提供事实性回答。"
    "请确认知识库中存在相关资料并授予读取权限后重试。"
)

_INTERNAL_CONTEXT = re.compile(
    r"(?:本公司|我们公司|咱们公司|公司内部|单位|知识库|内部资料|公司资料|公司数据|"
    r"人员信息|员工|同事|人事|部门|岗位|薪资|工资|绩效|报销|"
    r"公司(?:的)?(?:员工|人员|制度|政策|项目|业务|财务|客户|合同|资料|数据))",
    re.IGNORECASE,
)
_FACTUAL_INTENT = re.compile(
    r"(?:介绍|查询|查找|检索|读取|统计|汇总|名单|数量|多少|是谁|是什么|有哪些|"
    r"何时|什么时候|哪里|为何|为什么|情况|信息|资料|负责|属于|入职|出生|毕业|经历|"
    r"规定|要求|内容)",
    re.IGNORECASE,
)
_MUTATION_INTENT = re.compile(
    r"(?:新建|创建|修改|改写|追加|写入|删除|移动|重命名|生成|制作|保存|发布|更新|编辑)",
    re.IGNORECASE,
)
_ACTION_DEPENDS_ON_FACTS = re.compile(
    r"(?:查询|查找|检索|读取|统计|汇总|名单|数量|多少|是谁|是什么|有哪些|介绍)",
    re.IGNORECASE,
)
_PERSON_TARGET = re.compile(
    r"(?:员工|同事|人员|成员)(?:叫|姓名为|是)?\s*([\u3400-\u9fff]{2,4})(?=$|[的，。！？?\s])"
)
_FILE_TARGET = re.compile(
    r"([\w\u3400-\u9fff.-]+\.(?:pdf|txt|md|docx?|xlsx?|csv))",
    re.IGNORECASE,
)
_GENERIC_PERSON_WORDS = frozenset({
    "数量", "信息", "资料", "情况", "名单", "统计", "制度", "政策", "简历", "总数",
})
_FILE_LEADING_WORDS = re.compile(
    r"^(?:(?:请|麻烦|帮我|帮忙|读取|查看|查询|查找|打开|介绍|介绍一下)+)"
)
_PERMISSION_RESOURCE = r"(?:知识库|工作区|workspace)(?:的)?(?:文件|文档|资料)?"
_OWN_PERMISSION_QUESTION = re.compile(
    r"(?:请(?:问|说明|介绍(?:一下)?)\s*)?"
    r"(?:你|您|(?:当前|这个|该)\s*Agent)\s*(?:当前|目前|现在)?\s*"
    rf"(?:(?:对|在)\s*{_PERMISSION_RESOURCE}\s*)?"
    rf"(?:(?:有|拥有|具备)(?:哪些|什么|哪种)\s*(?:{_PERMISSION_RESOURCE})?权限(?:范围)?"
    rf"|的\s*(?:{_PERMISSION_RESOURCE})?权限(?:范围)?(?:是(?:什么|哪些))?)"
    r"\s*[？?。!！]*\s*",
    re.IGNORECASE,
)


def build_internal_grounding_policy(
    user_text: str,
    attachments: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Classify one request and identify any valid current-turn attachment evidence."""

    text = str(user_text or "").strip()
    has_internal_context = bool(_INTERNAL_CONTEXT.search(text))
    has_factual_intent = bool(_FACTUAL_INTENT.search(text))
    has_mutation = bool(_MUTATION_INTENT.search(text))
    mutation_only = has_mutation and not bool(
        _ACTION_DEPENDS_ON_FACTS.search(text)
    )
    # The trusted runtime policy is the source for the Agent's own permissions.
    # Match the whole question so mentioning permissions cannot exempt a mixed
    # request for employee details, document contents, or other internal facts.
    own_permission_question = bool(_OWN_PERMISSION_QUESTION.fullmatch(text))
    required = bool(
        text and has_internal_context and has_factual_intent
        and not mutation_only and not own_permission_question
    )
    target_terms = _target_terms(text, include_file_targets=not has_mutation) if required else ()
    attachment_sources = (
        _valid_attachment_sources(attachments, target_terms)
        if required
        else ()
    )
    return {
        "required": required,
        "target_terms": list(target_terms),
        "attachment_sources": list(attachment_sources),
    }


def workspace_read_evidence(
    tool_name: str,
    result_content: str,
    target_terms: Iterable[str] = (),
) -> dict[str, Any] | None:
    """Return safe provenance only for a relevant, successful workspace.read payload."""

    if str(tool_name or "") != "workspace.read":
        return None
    try:
        payload = json.loads(str(result_content or ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("tool_name") != "workspace.read":
        return None
    if payload.get("has_extractable_text") is False:
        return None
    logical_path = str(payload.get("logical_path") or "").strip()
    content = str(payload.get("content") or "").strip()
    if not logical_path or not content:
        return None
    relevant_text = f"{logical_path}\n{content}".casefold()
    normalized_terms = tuple(
        term.casefold() for term in (str(value or "").strip() for value in target_terms) if term
    )
    matched_terms = tuple(term for term in normalized_terms if term in relevant_text)
    if normalized_terms and not matched_terms:
        return None
    return {
        "logical_path": logical_path,
        "encoding": str(payload.get("encoding") or ""),
        "page_count": max(0, int(payload.get("page_count") or 0)),
        "matched_terms": list(matched_terms),
    }


def _target_terms(text: str, *, include_file_targets: bool) -> tuple[str, ...]:
    values: list[str] = []
    for match in _PERSON_TARGET.finditer(text):
        value = match.group(1).strip()
        if value not in _GENERIC_PERSON_WORDS:
            values.append(value)
    if include_file_targets:
        for match in _FILE_TARGET.finditer(text):
            value = _FILE_LEADING_WORDS.sub("", match.group(1).strip())
            values.append(value)
    return tuple(dict.fromkeys(value for value in values if value))


def _valid_attachment_sources(
    attachments: Iterable[Mapping[str, Any]],
    target_terms: tuple[str, ...],
) -> tuple[str, ...]:
    sources: list[str] = []
    for item in attachments or ():
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "attachment").strip()
        extracted_text = str(item.get("extracted_text") or "").strip()
        if not extracted_text:
            continue
        relevant_text = f"{name}\n{extracted_text}".casefold()
        if target_terms and not any(term.casefold() in relevant_text for term in target_terms):
            continue
        sources.append(name)
    return tuple(dict.fromkeys(sources))
