"""Immutable v1 system skill catalog shared by provisioning and its data migration.

Keep this module stable after migration 0041 has shipped. A future catalog revision
should use a new versioned module and migration instead of changing v1 defaults.
"""

import time


DEFAULT_SKILLS_V1 = (
    {
        "id": "general-contract-review",
        "name": "合同审查",
        "category": "法律",
        "priority": 10,
        "description": "结构化审阅合同主体、关键义务与风险条款，输出风险分级和可执行的修改建议。",
        "author": "claude-office-skills/skills",
        "usage": "在用户提供合同文本、附件或主要条款，并希望进行风险审阅、条款比较或修改建议时使用。",
        "instructions": (
            "确认合同类型、审查立场、签约主体及重点关注事项。",
            "提取金额、期限、交付、验收、付款、保密、知识产权、违约和争议解决等关键条款。",
            "对风险按高、中、低分级，引用对应条款并说明原因。",
            "区分缺失条款、表述歧义和权责失衡，给出可直接落地的修改建议。",
            "明确说明结果仅供业务与法务复核，不替代正式法律意见。",
        ),
        "source": "https://github.com/claude-office-skills/skills/tree/main/contract-review",
    },
    {
        "id": "general-finance-audit",
        "name": "财务审计与勾稽核对",
        "category": "金融",
        "priority": 20,
        "description": "对账表、流水与财务明细进行勾稽核对，识别差异、重复、遗漏及异常金额。",
        "author": "insight68/Skills",
        "usage": "在用户需要核对银行流水、总账、发票、应收应付或多份财务明细之间的一致性时使用。",
        "instructions": (
            "确认数据期间、币种、口径、主键和容差范围。",
            "清洗日期、金额、摘要、凭证号和交易对手等字段。",
            "执行一对一、一对多及多对一匹配，并保留匹配依据。",
            "分类输出未达项、重复项、金额差异、日期差异和疑似异常项。",
            "汇总核对覆盖率与差异金额，给出复核清单；不擅自修改原始账务数据。",
        ),
        "source": "https://github.com/insight68/Skills/tree/main/skills/finance-reconcile",
    },
    {
        "id": "general-data-analysis",
        "name": "数据分析",
        "category": "学术",
        "priority": 30,
        "description": "完成数据理解、清洗、探索、统计分析与结论表达，并记录口径和限制。",
        "author": "arjunprabhulal/agent-skills",
        "usage": "在用户提供表格、数据库结果或指标数据，希望发现趋势、比较差异、验证假设或形成分析报告时使用。",
        "instructions": (
            "先明确业务问题、指标定义、分析粒度和时间范围。",
            "检查字段类型、缺失值、重复值、异常值与样本偏差。",
            "选择与问题匹配的描述统计、分组比较、相关分析或可视化方法。",
            "将事实、推断和建议分开表达，避免把相关性解释为因果关系。",
            "输出可复现的步骤、关键结果、局限性和后续验证建议。",
        ),
        "source": "https://github.com/arjunprabhulal/agent-skills/tree/main/skills/data/data-analysis",
    },
    {
        "id": "general-market-research",
        "name": "市场研究",
        "category": "电商/营销",
        "priority": 40,
        "description": "围绕市场规模、客户需求、竞争格局与机会假设形成有来源、可追溯的研究结论。",
        "author": "xcrrr/claude-skills",
        "usage": "在用户需要行业扫描、竞品分析、用户画像、市场进入评估或商业机会验证时使用。",
        "instructions": (
            "明确研究问题、地域、时间范围、目标客户和交付格式。",
            "优先使用权威、近期且可追溯的公开来源，记录发布日期和链接。",
            "从市场、客户、竞品、渠道、价格及监管等维度组织证据。",
            "区分已验证事实、估算值与研究假设，说明估算方法。",
            "输出关键洞察、风险、机会及下一步验证动作。",
        ),
        "source": "https://github.com/xcrrr/claude-skills/tree/main/skills/business/market-researcher",
    },
    {
        "id": "general-meeting-minutes",
        "name": "会议纪要与行动项",
        "category": "办公写作",
        "priority": 50,
        "description": "将会议记录整理为议题、决策、行动项和待确认问题，便于跟进与追溯。",
        "author": "shangbianai/pm-meeting-notes",
        "usage": "在用户提供会议录音转写、聊天记录或零散笔记，并要求生成纪要、决策记录或任务清单时使用。",
        "instructions": (
            "保留会议主题、时间、参与人和背景信息。",
            "按议题归纳讨论要点，不虚构未出现的结论。",
            "单独列出已确认决策、未决问题和风险。",
            "行动项应包含负责人、任务、截止时间和依赖；缺失信息标记为待确认。",
            "对可能存在歧义的内容保留原意并提示人工复核。",
        ),
        "source": "https://github.com/shangbianai/pm-meeting-notes",
    },
)

DEFAULT_SKILL_IDS_V1 = tuple(skill["id"] for skill in DEFAULT_SKILLS_V1)


class DefaultSkillConflict(ValueError):
    """A custom skill occupies a reserved system skill ID."""


def _content(skill):
    instructions = "\n".join(
        f"{number}. {instruction}"
        for number, instruction in enumerate(skill["instructions"], start=1)
    )
    return (
        f'# {skill["name"]}\n\n'
        f'## 使用方法\n{skill["usage"]}\n\n'
        f'## 描述\n{skill["description"]}\n\n'
        f'## 指令\n{instructions}\n\n'
        f'## 来源\n- GitHub: {skill["source"]}\n'
        '- License: MIT\n'
        '- 适配说明: 基于开源项目的工作流思路重新整理为本平台通用技能，不包含原仓库脚本或依赖。\n'
    )


def ensure_default_skills_v1(transparent_company, skill_model, *, using="default"):
    """Insert missing system skills, preserving every existing skill and binding."""
    manager = skill_model.objects.using(using)
    timestamp = str(time.time())[:20]
    created_count = 0
    for skill in DEFAULT_SKILLS_V1:
        record, created = manager.get_or_create(
            transparentCompany_id=transparent_company.pk,
            skillId=skill["id"],
            defaults={
                "name": skill["name"],
                "version": "1.0.0",
                "category": skill["category"],
                "priority": skill["priority"],
                "enabled": True,
                "shortDescription": skill["description"],
                "author": skill["author"],
                "status": "active",
                "sourceType": "system",
                "runtimeStatus": "active",
                "isSystemSkill": True,
                "content": _content(skill),
                "updated_at": timestamp,
            },
        )
        if not created and (record.sourceType != "system" or not record.isSystemSkill):
            raise DefaultSkillConflict(
                f'Skill ID {skill["id"]} is occupied by a non-system skill'
            )
        created_count += int(created)
    return created_count
