"""内置初始数据：敏感规则（P0 固定 4 类）+ 转办映射（实施期由管理方确认后改表）"""
import json

from .models import ReferralMap, SensitiveRule

SENSITIVE_RULES = [
    {
        "name": "涉法涉诉",
        "patterns": ["起诉", "应诉", "答辩状", "上诉", "打官司", "判决书", "裁决", "诉讼时效", "传票", "开庭"],
        "advice": "该问题涉及法律纠纷与诉讼，建议前往乡镇司法所当面咨询，或拨打 12348 公共法律服务热线获取专业帮助。",
    },
    {
        "name": "医疗诊断",
        "patterns": ["吃什么药", "怎么治疗", "治疗方案", "确诊", "是不是癌", "病情严重吗", "打什么针", "开什么药", "用什么药"],
        "advice": "该问题涉及医疗健康，请前往乡镇卫生院或县级医院就诊，由医生当面诊断后给出治疗方案，切勿自行用药。",
    },
    {
        "name": "信访举报",
        "patterns": ["举报", "上访", "信访", "检举", "揭发", "告状", "纪检", "监察委"],
        "advice": "该问题涉及信访举报，请通过正规渠道反映：可联系村务监督委员会，或向县、乡纪检监察机关（12388）如实反映情况。",
    },
    {
        "name": "涉资金个人决策",
        "patterns": ["该不该借", "借钱给", "做担保", "帮我担保", "高利贷", "网贷", "理财靠谱", "投资什么", "该不该投", "存到哪家"],
        "advice": "该问题涉及个人资金往来决策，风险较高，建议前往银行网点或咨询乡镇财经部门，充分了解风险后审慎决定。",
    },
]

REFERRAL_MAP = [
    {"category": "办事流程", "org": "乡镇便民服务中心", "text": "建议携带相关证件前往乡镇便民服务中心窗口咨询办理。"},
    {"category": "惠民政策", "org": "乡镇民政/社保窗口", "text": "建议向乡镇民政办或社保窗口咨询，确认最新政策口径。"},
    {"category": "村务公开", "org": "村务监督委员会", "text": "建议向村务监督委员会或村委会查询相关公开信息。"},
    {"category": "生活服务", "org": "村委会综合服务窗口", "text": "建议联系村委会综合服务窗口咨询具体办理方式。"},
]

GENERIC_REFERRAL = {"org": "乡镇便民服务中心", "text": "建议向乡镇便民服务中心或相关部门当面咨询确认。"}


def seed_all(db) -> None:
    if db.query(SensitiveRule).count() == 0:
        for r in SENSITIVE_RULES:
            db.add(SensitiveRule(name=r["name"], patterns=json.dumps(r["patterns"], ensure_ascii=False),
                                 advice=r["advice"], enabled=1))
    if db.query(ReferralMap).count() == 0:
        for m in REFERRAL_MAP:
            db.add(ReferralMap(category=m["category"], org=m["org"], text=m["text"]))
