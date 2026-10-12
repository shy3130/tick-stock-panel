"""把一个交易日的资讯收成具体事件，而不是宽行业榜。

上一收盘（15:00）之后到下一交易日 9:25 的资讯归入下一交易日，和隔夜竞价是同一窗口。
周末打开页面时，这场开盘已经有稿就列出这些还在等待竞价的事件；还没有就显示上一个交易日。

聚类看标题里的主体、动作，以及标题里出现的细概念，再加同一时间窗。正文里抽到、
但标题没写的概念不参与聚类，避免旁支概念把无关消息粘成一件事。同一主体和动作，
或标题高度相似，会在较短时间窗内合并。动词前面那一段经常不是公司名（「公司董事被立案」、
「全资子公司拟收购」），标题里点名的上市公司仍算主体：以集团、股份结尾，左边是边界或
向、与、的这类衔接，并且名字里没有回购、立案这类动作。同一家公司、同一个动作、相差
不超过 6 小时，并成一件。分桶键用的就是这组主体，不能把该并的稿分到两个桶。
「A与B签署」和「B与A签署」是同一组双方，不分前后；双方和动作都相同的，合并窗口是
整个交易日（上一收盘到下一 9:25），不再卡 6 小时。摘要只在标题没有主体时参与，
避免各条资讯共用的页脚把整天粘成一件事。
宽行业名不参与聚类，也不拿去扩成分股。

展示主标题是簇里最可信的新闻原标题，只去掉来源前缀、括号、券商组名，以及开头的「报道」「据报道」「消息称」。
第一句完整时停在句号、分号、叹号、问号或全文结束处，不在 40 字或逗号处截开。第一句太短或没有主体时，把下一句留下。引号没合上时不在引号里截断。公司名后面的冒号留下公司名，后面的顿号按原文保留。
模型写一句话要点放在副标题。要点只丢掉空的、替换符或乱码、来源前缀和寒暄，不套主标题的主谓宾门槛，也不卡 12 到 18 字。数字、百分号和斜杠不算乱码。和主标题相同或已经包含在主标题里的要点不重复显示。
来源优先：公司公告和交易所，然后新华社、央视、证监会这类官方稿，再是财联社、华尔街见闻和格隆汇，然后外媒。
知识星球、ima、钉钉里带「更新」「建议」「把握机会」「1e+」或编号列表的研报标题，不当主标题。
公司或主题已经写清楚、并且过了原标题门槛的，可以用清理后的原标题。多主题晨报按条拆开，不再整篇丢掉。
有新闻源标题就用那条；没有就用过了严格门槛的模型标题；再没有就丢掉。
上一收盘之后到下一交易日 9:30，同一股票或同一细概念、并且是同一个动作的稿并成一件，不再卡 6 小时。
提价、涨价函、上调价格和涨价算同一个提价动作。公司提价至少是重要；写了幅度或再次提价就是重大。
隔夜和周末的新鲜度从该交易日 9:00 起算；簇里还有更晚的稿时，从最晚那条起算。
寒暄、纯点评、研报标签、空标题和只有栏目标签的，仍然不进列表。要点缓存版本仍是 v19。
半截标题并回完整事件，提及次数跟着走。
清理会去掉开头的钟点、【】标签、末尾省略号、已知媒体来源前缀（央视财经、财联社、新华社、经济日报），券商「招商电新…称」，以及研报文件名里的日期戳和 #。第一句太短或没有主体时，把叹号后面的下一句留下。引号没合上时不在引号里截断。
公司名后面的冒号不是来源。后面如果以「公司」或「本公司」起笔，就换成这家公司的名字。
栏目标题、只有来源名或公司名、把「前法官」写成「在前法官」的标题，以及只有点评、重点、没有动作的稿，不进列表。
侧面佐证、印证、看好、地位、逻辑、关注、把握这类没有主体和动作的点评不是事件。持续关注开头，以及 1）、① 这类列表残句，也不是事件。
国务院、证监会、工信部、发改委、国资委、央行或点名的公司后面，布局、建议、推荐是动作，不是点评。
增长、新增、追加这类业绩和回购动作算事件，但同比、数据显示这种没有具体主体的不算。
已经点名的公司不能换成「公司」，这个对照看原始标题，不看清掉冒号之后的句子。
签约、签署、战略合作要留下双方，不能只剩建发股份或只剩优必选。
动词贴在句首、后面又接上另一组主体和动作的，是语序颠倒，这种稿不进列表。
主标题保留第一整句，不在逗号处截开。只有「发布」没有宾语的短标签会跳过，改用下一条原标题。
这种半截标题不能再单独占一条，免得和完整事件重复。
标题末尾的券商组名（天风计算机）去掉。观点更新、话题用 & 拼起来的研报标签、
停在「近八」这种没写完的数量，以及只有产品名没有主体的「Cybercab扩容提速」，都不进列表。
奖项、文体、体育这类和资本市场无关、又没有 A 股映射的事件不进列表。

概念先留标题里已经出现的细概念，再用主体到 A 股概念的小词典补回被滤空的映射。
概念必须对得上这条事件自己的展示标题：同簇其他稿上的概念，以及只在其他稿里出现的词典触发词，不贴过来。
伊朗、中东映射到中东，不能因为都出现了「冲突」就标成俄乌。
「俄乌冲突快报」这类栏目名不是概念。事件自己没有概念时，模型只能从这张词典里选，而且触发词必须出现在这条标题里。词典没写到的无关概念仍然丢掉。
利好利空也只看这条标题，不靠同簇多数票，模型不能把已经判定的负面动作改成利好。
削减、砸单、砸价、下调、取消、暴跌、减产先于订单和发布，这些动作是利空。
只有标题写明利好某个名字（例如利好下游电池）时，那个受益方才标利好；没有这些词时仍默认利好。
同一时间窗里，财政部、国务院这类国内政策主体即使一个写发布、一个写出台，也并成一件事。
"""
from __future__ import annotations

import copy
import hashlib
import logging
import math
import re
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from functools import lru_cache

from app.market_time import CN_TZ, cn_now, current_trading_day
from app.news.collectors import is_red, parse_time
from app.news.extract import _usable_sector_name
from app.news.store import NewsStore

logger = logging.getLogger(__name__)

_LOOKBACK_DAYS = 21
_CACHE_SECONDS = 600
_CLUSTER_WINDOW = timedelta(hours=6)
_HEAT_HALF_LIFE_HOURS = 12
# 加红稿在热度里按 2.5 条计，普通稿仍按 1 条。
_RED_MENTION_WEIGHT = 2.5
# 要展示的事件一次批量起标题，不再逐条各问一次。
# 20 条一批（实测 40 条一批时，同一批里后段的条目模型会集中留空 —— 批太大反而
# 拉低覆盖率；20 条一批稳定在 17~20/20）。
_LLM_TITLE_BATCH = 20
# 40 条要点的 JSON 加正文输入放得下。正文进 prompt 后输入变长，4096 不够。
_LLM_TITLE_TOKENS = 8192
# 一批要点 + 正文，默认 30 秒不够，实测有整批返回空（疑似超时被截）。
# 放宽到 90 秒；单条调用仍用默认，不受影响。
_LLM_TITLE_TIMEOUT = 90.0
# 一条正文在 prompt 里最多带这么多字符，够放下数字和原因，又不至于把预算吃光。
_BODY_IN_PROMPT_CHARS = 800
_TITLE_LIMIT = 20
# 起标题的切片长度。展示顺序**先于**写要点定下来（见 _compute 注释），
# 所以切片只要盖住「正式展示的前 _TITLE_LIMIT 条」即可；留 4 倍余量是给
# 过滤/重排一点缓冲（实测最终前 20 的名次落在切片前 80 名以内）。
_TITLE_SLICE = _TITLE_LIMIT * 4
# 一批要点整批 title 全空时，原地重发的次数上限。
# 上游 content 网关偶发返回整批空 title（正文都在、只回骨架 JSON，或回满长度但
# title 字段全空），实测同一批原文重发就能恢复，单次恢复率约 50%。重发 3 次后
# 累计成功率 >93%，平均只花 1.5~2 次调用。**不拆半**：拆半后每半各自只有 50%
# 成功率，实测花 3~4 次调用才拿到 8~9/20，比原地重发（1~2 次拿 20/20）更贵更差。
_BLANK_RESEND = 3
# 多行资讯里「首行短得像标题」的判定上限。财经快讯的标题极少超过这个长度，
# 超过就认为首行本身也是正文（不能丢），交给按句子重合度的剥离逻辑处理。
_FIRST_LINE_TITLE_MAX = 40
# 展示用的分句可以比模型标题稍长，避免为了卡 20 字把宾语、数字或动词从中间切开。
_TITLE_HARD = 28
# 一句话要点仍用这道完整句检查。超过 48 字或短于 6 字直接丢掉。
_LLM_TITLE_MAX = 48
# 补充要点是「一行里挑 1~2 个最关键的新事实」，不是把正文缩写一遍。
# 实测复审反馈多条要点 80~150 字（东百集团把营收、净利润、业务说明全塞进去），
# 所以要点单独卡一道更短的上限：超过就丢弃，不做截断（截断容易留半句话，
# 正是要避免的）。prompt 里也同步要求「一句话、不超过 40 字」。
_SUBTITLE_MAX = 40
# 清理后的回退标题如果停在这些残词上，不能拿来顶替没通过的模型标题。
_FALLBACK_BAD_TAILS = ("集体", "进行", "中", "资", "的", "和", "与")
_FALLBACK_BAD_OPENINGS = ("市场没有", "我们认为", "点评")
_CJK_SLASH = re.compile(r"[\u4e00-\u9fff]\s*[/／]\s*[\u4e00-\u9fff]")
# 半截标题对照用。第一句通常到不了这么长；再长仍整句保留，页面两行截断。
_TITLE_DISPLAY_MAX = 80
# 标题缓存按这个版本取。漏编号被记成未采用的 v15 结果不再套用。
# v17: 要点新增长度上限与「事实必须出自正文」核对，旧缓存里超长/无据的要点作废。
# v18: 取文改成「单行快讯也算正文」（见 _body_from_text）。cls/wscn/reuters/
#      bloomberg 的快讯大量是单行，早先一律判无正文，这些条目被记成 _miss 缓存
#      下来；不升版本的话，明明已经能取到正文，仍会被旧 _miss 直接跳过 ——
#      线上表现为「切片 80 条里只有 3 条无正文，却仍有 7 条前 20 拿不到要点」。
# v19: 取文改成「候选与同簇候选比对」（见 _candidate_in_event）。早先拿候选
#      标题去比跨源重写过的主标题（Jaccard≥0.5），主标题措辞与任何候选都对不上，
#      被漏掉的 8 条事件正文一条都取不到；v18 把它们的失败写进了 _miss。
_TITLE_CACHE_VERSION = 19
_RESEARCH_SOURCES = frozenset({"dws", "zsxq", "ima"})
# 公司公告、交易所。sec 是公告原文；标题前缀里的交易所同样算这一档。
_FILING_SOURCES = frozenset({"sec"})
# 财联社、华尔街见闻、格隆汇同一档。ETF 资金流没单独点名，也放在这里。
_WIRE_SOURCES = frozenset({"cls", "wscn", "glh", "etf_flow"})
_FOREIGN_SOURCES = frozenset({
    "reuters", "bloomberg", "wsj", "cnbc", "marketwatch", "scmp",
})
# 群聊、星球、ima。Reddit 讨论和群聊同一档。
_CHAT_SOURCES = frozenset({"dws", "zsxq", "ima", "reddit"})
_EXCHANGE_MARKS = (
    "上交所", "深交所", "北交所", "港交所",
    "上海证券交易所", "深圳证券交易所", "北京证券交易所", "香港交易所",
    "公司公告",
)
_OFFICIAL_MARKS = (
    "新华社", "人民日报", "央视新闻", "央视财经", "央视",
    "证监会", "国务院", "中国人民银行", "央行", "发改委", "国家发展改革委",
    "工信部", "财政部", "国资委", "商务部", "政治局", "中共中央",
    "金融监管总局", "银保监会",
)
_GATE_EXTRA_VERBS = ("发起", "宣布")
_CHATTER_MARKS = ("早上好", "早安", "中午好", "下午好", "晚上好", "晚安", "大家好", "各位")
_NARRATIVE_MARKS = ("利空叙事", "利好叙事", "叙事在")
# 这些词当主语时还不是机构、公司或商品。美联储可能降息仍算有主体。
_GATE_HEDGES = ("可能", "或许", "还会", "几次", "多次", "预计", "有望", "或将", "料将")
# 数字后面的单字量词是写完了，不是把词切断。例如 13.1元。
_MEASURE_TAILS = frozenset("元吨点股成倍")
_CN_NUMERALS = frozenset("一二三四五六七八九十百千万两")
# 以数字开头、但其实是日期的标题：「10月以来…」「12月3日…」都是正常新闻。
# 「以数字开头」这条门槛本来要拦的是「1. 」「2）」这类编号清单。
_DATE_LIKE_OPENING = re.compile(r"^\d{1,2}\s*[月日]")
# 词典词性里能当主体的标记。普通名词不算，避免「数据」混进去。
_POS_ENTITY_FLAGS = frozenset({"ns", "nr", "nt", "nz", "j"})
_POS_ENTITY_MARKS = ("价格", "股价", "公司", "股份", "集团", "指数", "电网", "机场", "银行")
_TRAILING_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9]*$")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_JIEBA = None
_CONCEPT_LIMIT = 4

# 申万一级和口头上的大板块。出现在标题里也不够把两条资讯收成同一事件。
BROAD_SECTORS = frozenset({
    "人工智能", "机器人", "半导体", "新能源", "医药", "银行", "白酒", "军工",
    "消费", "地产", "房地产", "券商", "证券", "有色", "煤炭", "钢铁", "电力",
    "汽车", "通信", "计算机", "电子", "传媒", "农业", "化工", "机械", "建筑",
    "交运", "家电", "商贸", "纺织", "轻工", "综合", "公用事业", "非银金融",
    "国防军工", "美容护理", "社会服务", "环保", "石油石化", "基础化工",
    "有色金属", "交通运输", "建筑材料", "建筑装饰", "商贸零售", "纺织服饰",
    "轻工制造", "电力设备", "机械设备", "农林牧渔", "医药生物", "家用电器",
    "食品饮料",
})
_ACTIONS = (
    "发布", "出台", "定调", "涨价", "降价", "提价", "上调", "下调", "报价", "收购", "中标", "获批", "上市",
    "回购", "签约", "签署", "签订", "停产", "召回", "立案", "补贴", "降准", "降息", "加息", "制裁", "收紧",
    "追加", "订单", "增长", "新增", "加速释放",
)
# 先看更具体的市场语境，再退回动作本身。六个分类覆盖政策、海外、产业、地缘、商品和公司。
CATEGORY_NAMES = (
    "国内政策/宏观",
    "海外市场/央行",
    "科技与产业",
    "地缘政治",
    "大宗商品/期货价格异动",
    "公司重大事项",
)
_CATEGORY_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("地缘政治", ("制裁", "关税", "冲突", "战争", "停火", "出口管制", "配额", "地缘")),
    ("海外市场/央行", ("美联储", "欧央行", "日本央行", "英央行", "加息", "降息", "美股", "纳指", "标普", "欧股", "日经")),
    ("国内政策/宏观", ("出台", "补贴", "降准", "国务院", "发改委", "工信部", "证监会", "财政部", "国资委", "央行", "宏观", "政策")),
    ("大宗商品/期货价格异动", ("碳酸锂", "原油", "期货", "黄金", "铜价", "铁矿", "煤炭", "稀土", "豆粕", "螺纹", "报价", "橡胶")),
    ("公司重大事项", ("收购", "回购", "上市", "立案", "停产", "召回", "签约", "签署", "签订", "中标", "停牌", "退市")),
    ("科技与产业", ("发布", "订单", "追加", "获批", "涨价", "降价", "提价", "上调", "下调", "量产", "芯片", "模型")),
)
_ACTION_CATEGORY = {
    "出台": "国内政策/宏观",
    "定调": "国内政策/宏观",
    "补贴": "国内政策/宏观",
    "降准": "国内政策/宏观",
    "加息": "海外市场/央行",
    "降息": "海外市场/央行",
    "制裁": "地缘政治",
    "收紧": "地缘政治",
    "报价": "大宗商品/期货价格异动",
    "收购": "公司重大事项",
    "回购": "公司重大事项",
    "上市": "公司重大事项",
    "立案": "公司重大事项",
    "停产": "公司重大事项",
    "召回": "公司重大事项",
    "签约": "公司重大事项",
    "中标": "公司重大事项",
    "增长": "公司重大事项",
    "新增": "公司重大事项",
}
_BEAR_CUES = ("制裁", "立案", "停产", "召回", "下调", "降价", "收紧", "冲突", "战争", "关税", "暴跌", "下跌", "加息")
_BULL_CUES = ("补贴", "获批", "订单", "涨价", "回购", "发布", "出台", "降息", "降准", "上调", "提价", "中标", "签约", "上涨")
_NOISE_EXACT = frozenset({"图片", "广告", "视频", "转发", "分享", "推广", "赞助"})
_NOISE_HINTS = ("闲聊", "广告", "推广", "赞助", "加微信", "点击领取", "转发微博")
_RELEVANCE_MIN = 3
# 重要性高于映射、新鲜度和热度。热度封顶，避免条数把寻常涨价顶过降息或政策。
# 盘面验证另计，权重和重要性同级，不放进这个封顶。
_IMPORTANCE_POINTS = {"琐碎": 0, "一般": 20, "重要": 55, "重大": 130}
_IMPORTANCE_ORDER = ("琐碎", "一般", "重要", "重大")
_MAPPING_CONCEPT = 15
_MAPPING_ASSET = 15
_MAPPING_DIRECTION = 5
_FRESH_MAX = 20
_HEAT_BONUS_CAP = 15
_MAJOR_RATE = ("降息", "加息", "降准", "美联储", "欧央行", "日本央行", "英央行")
_MAJOR_ORGS = ("国务院", "发改委", "工信部", "证监会", "财政部", "央行")
_MAJOR_POLICY_ACTS = ("出台", "补贴", "政策", "降准")
_MAJOR_GEO = ("制裁", "战争", "关税", "出口管制", "配额")
_MAJOR_COMMODITIES = ("碳酸锂", "原油", "黄金", "铜价", "铁矿")
_PRICE_VERBS = ("报价", "涨价", "降价", "提价", "上调", "下调", "上涨", "下跌", "暴涨", "暴跌")
_LEADERS = (
    "华为", "宁德时代", "贵州茅台", "英伟达", "苹果", "特斯拉", "比亚迪",
    "平安银行", "腾讯", "阿里巴巴", "茅台",
)
# 标题里点名的机构。缺主体时从代表标题里补这些，而不是留下「可能还会」「运行情况」。
_ORG_SUBJECTS = (
    "美联储", "欧洲央行", "欧央行", "日本央行", "英格兰银行", "英央行",
    "国务院", "政治局", "发改委", "工信部", "证监会", "财政部", "国资委",
    "商务部", "外交部", "住建部", "国家能源局", "能源局", "国常会",
    "央行",
)
_EXPLICIT_SUBJECTS = tuple(sorted(set(_ORG_SUBJECTS) | set(_LEADERS), key=len, reverse=True))
# 没有具体对象的词。不能单靠它们把两条资讯收成一件事。
_GENERIC_TOKENS = frozenset({
    "超预期", "不及预期", "预期", "公告", "消息", "行情", "价格", "股价",
    "政策", "市场", "公司", "股份", "板块", "概念", "产业链", "产业",
    "上涨", "下跌", "大涨", "大跌",
})
# 概念名和标题只共用这些词时，不能当成同一件事。冲突两个字会把伊朗稿贴上俄乌。
_CONCEPT_GLUE = frozenset({
    "冲突", "战争", "危机", "局势", "问题", "方面", "情况", "事件",
    "最新", "焦点", "热点", "快报", "解决", "构想", "消息",
})
_SUBJECT_TRAIL = (
    "价格", "股价", "期货", "行情", "板块", "概念", "产业链", "产业",
    "公司", "集团", "股份", "厂商", "企业",
)
# 这些词当主语时，标题还缺一个能指认的主体。
_VAGUE_CUES = (
    "可能", "或许", "预计", "有望", "或将", "料将",
    "还会", "还将", "几次", "多次",
    "情况", "谋划", "宏观",
    "进一步", "消息称", "据悉", "传闻称",
)
_SIMILAR_WINDOW = timedelta(hours=3)
_SIMILARITY_MIN = 0.72
_REPRESENTATIVE_SIMILARITY = 0.45
_LEADER_ACTS = ("发布", "收购", "回购", "停产", "制裁", "订单", "上市", "立案")
_IMPORTANT_ACTS = (
    "涨价", "提价", "涨价函", "提价函", "上调价格", "价格上调", "上调报价",
    "获批", "回购", "订单", "收购", "中标",
)
# 「上调产品价格」中间可以隔几个字，但「上调评级」不是提价。
_HIKE_PATTERN = re.compile(
    r"涨价函|提价函|涨价|提价|上调[^，。；！？\n]{0,8}价格|价格上调|上调报价|上调出厂价"
)
_HIKE_PERCENT = re.compile(r"\d+(?:\.\d+)?\s*[%％]|百分之|\d+成")
_HIKE_REPEAT = (
    "再次", "再度", "二次", "又一次", "第二次", "第2次",
    "连续提价", "连续涨价", "继续提价", "继续涨价",
)
_COMPANY_SUFFIX = re.compile(r"[\u4e00-\u9fff]{2,12}(?:科技|股份|集团|公司)")
# 晨报里的 1、 / ①。至少两条才拆，避免把普通句子里的数字切开。
_BRIEF_MARK = re.compile(
    r"(?:^|[\n；;])\s*(?:[（(]?[0-9]{1,2}[)）、.．]|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫])\s*"
)
# 切片之外仍要优先起标题的条数。只替换已经有原标题的位置，模型调用次数不增加。
_TITLE_PRIORITY_EXTRA = 20
_IMPORTANT_CATEGORIES = frozenset({
    "国内政策/宏观", "海外市场/央行", "地缘政治", "公司重大事项",
})
_LEAD_SUFFIXES = ("宣布", "称", "表示", "指出", "消息", "传闻", "公司", "披露")
_STOP = frozenset({
    "公司", "市场", "今日", "表示", "消息", "记者", "财经", "股份", "有限",
    "集团", "板块", "概念", "龙头", "资金", "大涨", "大跌", "上涨", "下跌",
    "走强", "走弱", "午后", "盘中", "持续", "继续", "相关", "中国", "美国",
    "国内", "海外", "全球", "多家", "有关", "A股", "港股",
})
# 两个字就能对上的行情套话。不能靠「美元」「价格」把石油概念贴到目标价上调上。
_CONCEPT_LINK_SKIP = _GENERIC_TOKENS | _STOP | _CONCEPT_GLUE | frozenset({
    "美元", "目标", "价格", "预期", "上调", "下调", "美股", "发布", "订单",
    "全球", "同比", "环比",
})
_CLAUSE = re.compile(r"[，。！？、；：:\n]")
# 半句话常见的起笔。完整标题不会从这些词开始。
_FRAGMENT_LEADS = (
    "再叠加", "叠加", "再加上", "加上",
    "和之前", "和此前", "和这次", "和本次", "和上述", "以及",
    "根据", "据悉", "据称", "据报道",
    "与此同时", "与此",
    "同时", "此外", "另外", "并且", "而且",
    "但是", "不过", "然而", "虽然", "尽管", "如果", "因为", "由于", "随着",
    "其中", "对此", "为此", "因此", "因而",
    "继而", "随后", "此后", "日前", "此前", "还有",
)
_FRAGMENT_PREFIX = re.compile("^(?:" + "|".join(_FRAGMENT_LEADS) + ")")
_TIME_PREFIX = re.compile(r"^(?:昨夜|昨晚|今日|昨日|今天|昨天|日前|此前|本次|这次|当前|目前|最近|刚刚)+")
# 涨跌叙述不是聚类动作，但标题被从中间截开时，它往往才是这件事的谓语。
_PRICE_NARRATIVE = (
    "大涨", "大跌", "上涨", "下跌", "暴涨", "暴跌",
    "走强", "走弱", "走高", "走低", "飙升", "跳水",
    "砸价", "砸单",
)
# 聚类停用词里的国别可以当标题主体。这里只挡「公司」「市场」这种没有对象的词。
_NOT_SUBJECT = frozenset({
    "公司", "市场", "今日", "表示", "消息", "记者", "财经", "股份", "有限",
    "集团", "板块", "概念", "龙头", "资金", "多家", "有关", "相关",
    "持续", "继续", "午后", "盘中",
})
# 叙述谓语后面再接这些动作，就是两件不相干的事被粘在了一起。
_GLUE_FOLLOWUPS = frozenset({
    "发布", "出台", "收购", "中标", "获批", "上市", "回购", "签约",
    "停产", "召回", "立案", "补贴", "制裁", "收紧", "降息", "加息", "降准",
})
_DANGLING_FINAL = frozenset("的了和与及等把被将已正而或但之以为于对从在超")
# 以这些字结尾时，只有最后两个字是完整词才算没截断。
_CLOSED_TAILS = {
    "政": frozenset({"新政", "政府", "财政", "行政", "市政", "内政", "党政", "军政"}),
}
_SOURCE_HEADS = (
    "财联社", "华尔街见闻", "证券时报", "上海证券报", "中国证券报",
    "第一财经", "澎湃新闻", "新浪财经", "新华社", "界面新闻",
    "央视财经", "格隆汇", "金十数据", "金十", "经济日报",
    "路透社", "路透", "彭博社", "彭博", "南华早报",
)
_SOURCE_NAMES = frozenset(_SOURCE_HEADS)
_SOURCE_ALT = "|".join(sorted(_SOURCE_NAMES, key=len, reverse=True))
_SOURCE_BRACKET = re.compile(r"^(?:【[^】]{1,16}】|\[[^\]]{1,16}\]|（[^）]{1,12}）|\([^)]{1,12}\))")
# 残缺的前导标签，例如「新增隐债】」。成对的【】在后面整段去掉。
_BROKEN_BRACKET = re.compile(r"^[^【】\[\]]{1,16}[】\]]")
_BRACKET_PAIR = re.compile(r"【[^】]*】|\[[^\]]*\]")
_STRAY_BRACKET = re.compile(r"[【】\[\]]")
# 双字动词被从中间切开时，标题会停在第一个字上，例如「出台」变成「出」。
_VERB_PAIRS = ("出台", "发布", "上调", "下调", "降息", "加息", "推出", "宣布")
# 国内政策机构。同一窗口里动词不同（发布/出台）仍视为同一件事。央行利率不在这里。
_POLICY_ORGS = (
    "财政部", "国务院", "政治局", "发改委", "工信部", "证监会",
    "商务部", "央行", "国常会",
)
# 标题里点了主体、但提及被相关性滤空时，用这张表补 A 股概念。键必须同时出现。
_SUBJECT_CONCEPTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("橡胶",), ("橡胶", "天然橡胶")),
    (("原油",), ("石油", "油服")),
    (("IEA",), ("石油", "油服")),
    (("美联储", "加息"), ("黄金", "银行")),
    (("伊朗",), ("中东",)),
    (("以色列",), ("中东",)),
    (("加沙",), ("中东",)),
    (("巴勒斯坦",), ("中东",)),
    (("中东",), ("中东",)),
)
# 概念里点名的地缘实体必须在事件正文里出现。同一组里任一说法都算对上。
_GEO_ENTITY_GROUPS = (
    ("俄乌", "乌克兰"),
    ("伊朗", "中东", "以色列", "加沙", "巴勒斯坦"),
    ("俄罗斯",),
)
# 没有 A 股映射时直接丢掉。发布、获奖这类弱动词留不住诺贝尔和世界杯。
_HARD_NON_MARKET = (
    "诺贝尔", "奥斯卡", "金球奖", "格莱美", "金像奖", "金马奖", "艾美奖", "普利策", "图灵奖",
    "世界杯", "欧冠", "奥运会", "冬奥会", "亚运会", "欧洲杯", "亚洲杯",
    "英超", "西甲", "德甲", "意甲", "法甲", "中超", "NBA", "CBA",
)
# 政策、价格里顺带提到演出时仍保留。纯文体稿没有这些市场动作就丢掉。
_SOFT_NON_MARKET = ("演唱会", "电影节", "新专辑")
_STRONG_MARKET = (
    "出台", "补贴", "降准", "降息", "加息", "涨价", "降价", "提价", "上调", "下调",
    "回购", "收购", "立案", "制裁", "定调", "政策",
    *_PRICE_NARRATIVE,
)
_SOURCE_DATELINE = re.compile(
    r"^(?:" + _SOURCE_ALT + r")(?:\d{1,2}月\d{1,2}日)?(?:电|讯)[，,]?"
)
_SOURCE_LABEL = re.compile(
    r"^(?:(?:" + _SOURCE_ALT + r")(?:报道|快讯|讯|电)?|快讯|突发|独家|刚刚)[：:|｜]"
)
# 招商电新称、招商电新锂电行业更新称。监管机构研究称不是券商前缀。
_BROKER_NOTE = re.compile(
    r"^(?!(?:国务院|发改委|工信部|证监会|财政部|央行|国资委|商务部|美联储|政治局|欧央行))"
    r"[\u4e00-\u9fff]{2,8}(?:电新|计算机|研究)[\u4e00-\u9fffA-Za-z0-9]{0,16}称"
)
# 经济日报有时直接粘在标题前面，没有冒号。
_LEADING_ECONOMIC_DAILY = re.compile(r"^经济日报")
# 末尾或冒号前的组名。天风计算机、招商电新。没有这些券商名时，不因为句子里有「计算机」就删。
_BROKER_HOUSES = (
    "天风", "招商", "中信", "华泰", "中金", "国泰君安", "海通", "广发", "兴业",
    "申万", "国信", "安信", "方正", "长江", "东吴", "浙商", "中泰", "国金", "光大",
)
_BROKER_DESKS = ("计算机", "电新", "研究", "策略")
_BROKER_HOUSE = "|".join(sorted(_BROKER_HOUSES, key=len, reverse=True))
_BROKER_DESK = "|".join(_BROKER_DESKS)
_BROKER_TEAM_LEAD = re.compile(rf"^(?:{_BROKER_HOUSE})(?:{_BROKER_DESK})[：:]")
_BROKER_TEAM_TAIL = re.compile(rf"(?:{_BROKER_HOUSE})(?:{_BROKER_DESK})$")
# PCB设备&钓针观点更新这类研报标签。普通标题里的斜杠另有规则。
_TOPIC_JOIN = re.compile(r"[\u4e00-\u9fff][&＆][\u4e00-\u9fff]")
# 停在「近八」而后面没有单位，是半截数量，不是「近八成」或「降息25基点」。
_MID_PHRASE_CUT = re.compile(r"(?:近|约|逾)[0-9０-９一二三四五六七八九十]+$")
# 只有拉丁产品名加扩容、提速、加速落地，没有中文主体。
_SUBJECTLESS_PRODUCT = re.compile(
    r"^[A-Za-z][A-Za-z0-9]*(?:扩容|提速|加速|放量|提产|落地|推进)+$"
)
# 发布、出台这类及物动词没有宾语时，不能拿来替代更完整的原标题。
_OBJECT_VERBS = ("发布", "出台", "宣布", "签署", "签约", "签订", "推出", "公布", "披露", "印发")
_EARNINGS_CUES = ("业绩预告", "净利润", "扣非", "预告", "业绩", "净利", "营收")
_PAREN_NOTE = re.compile(r"（[^）]*）|\([^)]*\)")
_REPORT_DIANPING = re.compile(r"(?<=(?:业绩|预告|财报|年报|中报|季报))点评")
# 1）和①是列表残句，不是事件标题。
_LIST_MARKER = re.compile(r"(?:[0-9]+[)）]|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫])")
_REUTERS_TAIL = re.compile(r"(?:[-|｜]\s*)?(?:Reuters|路透社|路透)$")
# 15:32【财联社】被冒号切开后，会剩「32【…】」这种钟点碎片。
_CLOCK_PREFIX = re.compile(r"^(?:\d{1,2}[:：]\d{2})+")
_MINUTE_BEFORE_BRACKET = re.compile(r"^\d{1,2}(?=【)")
_LEFTOVER_MINUTE = re.compile(r"^\d{1,2}(?![年月日号点时分])(?=[\u4e00-\u9fff])")
_TRAILING_ELLIPSIS = re.compile(r"(?:\.{2,}|…+|。{2,})+$")
_SENTENCE_END = re.compile(r"[。；！？\n]")
# 机器翻译把 former judge 写成「在前法官」。只拦紧挨着职务的「在前」，
# 「在前三季度」「在前述」是正常说法。
_FORMER_OFFICES = (
    "副董事长", "副总统", "副总理", "副部长", "董事长", "发言人",
    "总统", "首相", "总理", "主席", "财长", "外长", "防长", "行长",
    "部长", "法官", "局长", "省长", "市长", "州长", "议长", "院长",
    "署长", "书记", "总裁", "董事", "首席", "大使", "国王", "女王", "元首",
    "CEO", "CFO", "CTO", "COO",
)
_BROKEN_FORMER = re.compile(
    r"在前(?:" + "|".join(_FORMER_OFFICES) + r")",
    re.IGNORECASE,
)
_FILE_EXT = re.compile(r"(?i)\.(?:pdf|docx?|xlsx?|pptx?|txt|csv)$")
_COMPACT_DATE = r"20\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"
_DASHED_DATE = r"20\d{2}[-./](?:0?[1-9]|1[0-2])[-./](?:0?[1-9]|[12]\d|3[01])"
# 文件名日期：前面是连字符，或后面紧跟 #。正文里的「央行2026-10-09」不在这里。
_FILE_DATE = re.compile(
    rf"(?:(?<=[-_—–])(?:{_COMPACT_DATE}|{_DASHED_DATE})(?!\d)"
    rf"|(?<!\d)(?:{_COMPACT_DATE}|{_DASHED_DATE})(?=[#＃]))"
)
_REPORT_KINDS = ("更新", "报告", "研报", "点评", "周报", "日报", "月报", "纪要", "简报")
_REPORT_PREFIX = re.compile(
    r"^(?:[\u4e00-\u9fffA-Za-z0-9]{1,24}(?:" + "|".join(_REPORT_KINDS) + r"))"
    r"[-_—–]+"
    r"(?:20\d{6}|20\d{2}[-./](?:0?[1-9]|1[0-2])[-./](?:0?[1-9]|[12]\d|3[01]))"
    r"[-_—–]*"
)
_SECTION_MATERIAL = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,12}材料$")
# 价格叙述后面再出现这些动作，就是下一句被粘进了标题。
_UNGLUE_VERBS = _GLUE_FOLLOWUPS | frozenset({"涨价", "降价", "提价"})
# 没有具体主体和动作时，这些是点评，不是事件。
_COMMENTARY_CUES = (
    "侧面佐证", "印证", "利好", "看好", "地位", "逻辑",
    "关注", "建议", "推荐", "布局", "把握",
)
# 机构或公司名后面，这三个词是政策或公司动作，不是点评。
_DIRECTIVE_VERBS = ("布局", "建议", "推荐")
_VAGUE_ACTORS = frozenset({
    "机构", "研报", "分析", "分析师", "券商", "行业", "板块", "市场",
    "资金", "投资者", "业内", "人士", "专家", "报告", "点评", "策略",
    "继续", "建议", "推荐", "布局", "关注", "把握",
})
# 模型若把「立讯精密」写成「公司」或「本公司」，标题不能用。
_GENERIC_SUBJECT_HEADS = ("本公司", "公司", "企业", "厂商", "集团")
# 叠加、接续把原因和事件粘成一句时，只留后面的核心事件。
_RUNON_GLUE = ("再叠加", "叠加", "接续", "再加上", "加上", "以及", "并且", "同时")
_CAUSE_TAILS = ("需求", "压力", "影响", "带动", "推动", "背景", "因素")
# 增长前面只有这些词时，还没有具体主体。
_STAT_PREFIXES = ("数据显示", "统计显示", "据统计", "同比", "环比")

_CACHE: dict[tuple, dict] = {}
_LLM_CACHE: dict[str, dict] = {}
# `_miss`（问过但没拿到要点）缓存的有效期。同一批原文重发就能恢复的情况不少，
# 永久跳过等于把一次偶发失败钉死到底；而且上游/正文侧改善后本可以写出来。
# 缓存键是簇成员标题的 sha1（`_fp`），簇不变就一直命中 —— 所以窗口必须**短于**
# 热点刷新周期（HOT_REFRESH_SECONDS=300），否则一个偶发空返回会在接连两个周期
# 里被跳过，线上表现为「同一批素材、前 20 要点覆盖率在两个周期里都是 13/20」。
# 取 240 秒：同一周期内不重复问（省一次 LLM 调用），下个周期必定重试。
_MISS_TTL_S = 240.0


def clear_hot_event_cache(*, llm: bool = True) -> None:
    _CACHE.clear()
    if llm:
        _LLM_CACHE.clear()
    _compact.cache_clear()
    _strip_source_prefix.cache_clear()
    _norm_title.cache_clear()
    _title_grams.cache_clear()


def build_top_hot_events(
    now: datetime | None,
    store: NewsStore,
    *,
    refresh: bool = False,
    blocked: bool = True,
) -> dict:
    now = (now or cn_now()).astimezone(CN_TZ)
    # blocked 放进键里，对照测试才能分别拿到分桶结果和两两比较结果。
    key = (str(store.path), int(now.timestamp()) // _CACHE_SECONDS, blocked)
    if not refresh:
        cached = _CACHE.get(key)
        if cached is not None:
            return copy.deepcopy(cached)
    snapshot = _compute(now, store, blocked=blocked)
    _CACHE[key] = snapshot
    return copy.deepcopy(snapshot)


def _active_session_day(now: datetime) -> date:
    """当前这份榜对应的交易日。周末是下一场开盘，工作日是当天。"""
    day = now.astimezone(CN_TZ).date()
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _news_session_day(published: datetime) -> date:
    """资讯归入哪一个交易日的热门榜。和竞价确认窗口同一口径。"""
    from app.news.market_confirm import _confirm_day

    return _confirm_day(published)


def _auction_still_ahead(now: datetime, chosen: date) -> bool:
    local = now.astimezone(CN_TZ)
    return local < datetime.combine(chosen, dt_time(9, 25), CN_TZ)


def _attach_limit_reasons(events: list[dict], chosen: date) -> None:
    """用当天涨停原因核对概念。没有分区或读失败时保持原事件。"""
    try:
        from pathlib import Path

        from app.config import settings
        from app.limit_board.enrich import apply_limit_reasons

        apply_limit_reasons(events, Path(settings.data_dir), chosen.isoformat())
    except Exception:  # noqa: BLE001
        logger.info("涨停原因未能核对热门事件", exc_info=True)


def _compute(now: datetime, store: NewsStore, *, blocked: bool = True) -> dict:
    today = now.date()
    trading = current_trading_day(now)
    active = _active_session_day(now)
    start = datetime.combine(today - timedelta(days=_LOOKBACK_DAYS), dt_time.min, CN_TZ)
    end = datetime.combine(today + timedelta(days=1), dt_time.min, CN_TZ)
    by_day: dict[date, list[dict]] = defaultdict(list)
    for row in store.items_between(start, end):
        published = parse_time(row.get("published_at"))
        if published is None:
            continue
        published = published.astimezone(CN_TZ)
        # 还没发布的稿不进榜，避免周末提前看到当天更晚的快讯。
        if published > now:
            continue
        row = dict(row)
        row["published"] = published
        for piece in _expand_topic_rows(row):
            by_day[_news_session_day(published)].append(piece)
    ranked = {day: _cluster_day(rows, now, blocked=blocked) for day, rows in by_day.items()}
    ranked = {day: events for day, events in ranked.items() if events}
    fallback = False
    if active in ranked:
        chosen: date | None = active
    elif trading in ranked:
        # 周末还没有隔夜稿时，继续显示上一个交易日，不算回退。
        chosen = trading
    else:
        earlier = [day for day in ranked if day < active]
        chosen = max(earlier) if earlier else None
        fallback = chosen is not None
    if chosen is None:
        return {
            "as_of": None,
            "trading_day": trading.isoformat(),
            "fallback": False,
            "hint": None,
            "updated_at": None,
            "events": [],
        }
    events = ranked[chosen]
    _attach_limit_reasons(events, chosen)
    # 第一次验证在写要点之前。新进前排的事件这时常常还没有个股或概念。
    mapped_before = {id(event): _confirmation_mapping(event) for event in events}
    from app.news.market_confirm import attach_confirmations
    attach_confirmations(events, now)
    events = _rank_events(events, now, grade=False)
    for event in events:
        _ensure_shown_name(event)
    events = _absorb_cutoff_events(events)
    # 先留下能上屏的，再写要点。过不了门槛的簇不再占模型名额。
    events = [
        event for event in events
        if (
            _keep_shown_event(event)
            or _awaiting_news_title(event)
            or _has_stock_mapping(event)
        )
    ]
    if not events:
        return {
            "as_of": None,
            "trading_day": trading.isoformat(),
            "fallback": False,
            "hint": None,
            "updated_at": None,
            "events": [],
        }
    # 先定展示顺序，再写要点 —— 顺序必须是**写要点之前**就定下来的那一份。
    # 原因：写要点会给事件补 concepts/stocks/direction，_score_event 里这些进
    # mapping 分，分数一变名次就变。若先写要点再排序，写过的条目会集体涨分、
    # 把没写过的挤到后面去；而「谁该写要点」又是按排序切的前 80 名决定的，
    # 两者互为因果 —— 实测最终前 20 名里有 12 条的名次落在切片之外，永远拿
    # 不到要点（点开是空白）。改成一个方向的流程：排序 → 切片 → 写要点 →
    # 只做过滤不再重排，前 20 名一定在切片内。
    #
    # 切片前先重排一次：_absorb_cutoff_events 把半截簇并进宿主后 heat/mentions
    # 变了，沿用合并前的顺序会让个别该进前 20 的事件沉下去。
    events = _rank_events(events, now, grade=False)
    # 竞价分已经算进名次。股票映射或重要、但还没有原标题的簇，优先占用切片名额。
    _label_with_llm(_events_for_titles(events))
    for event in events:
        _ensure_shown_name(event)
    # 写要点会经 _apply_llm 改掉 importance（模型给的级别），而 breakdown/score
    # 还是上面按规则级别算的。这里只把 importance 这一项和总分对齐，其余分项
    # 不动：mapping 会随 concepts/stocks 变，一起重算就会让名次跟着写没写过
    # 要点漂移，打破上面「前 20 必有要点」的保证。
    for event in events:
        _resync_importance_score(event)
    # 过滤会改变成员但不重排：名次以上一步为准，保证「前 20 有要点」成立。
    kept = [event for event in events if _keep_shown_event(event)]
    if not kept:
        return {
            "as_of": None,
            "trading_day": trading.isoformat(),
            "fallback": False,
            "hint": None,
            "updated_at": None,
            "events": [],
        }
    # 要点可能刚补上映射。趁 _first 还在，同一轮就把盘面验证补上，不改已经排好的顺序。
    _confirm_newly_mapped(kept, now, mapped_before)
    latest = max(event.pop("_latest") for event in kept)
    for event in kept:
        event.pop("_fp", None)
        event.pop("_first", None)
        event.pop("_batch_id", None)
        event.pop("_llm_title_applied", None)
        event.pop("_candidates", None)
    updated_hm = latest.astimezone(CN_TZ).strftime("%H:%M")
    return {
        "as_of": chosen.isoformat(),
        "trading_day": (chosen if not fallback else trading).isoformat(),
        "fallback": fallback,
        "hint": _hint(now, trading, chosen, fallback, updated_hm),
        "updated_at": updated_hm,
        "events": kept,
    }


def _prepare_merge_doc(doc: dict) -> None:
    """合并要用的主体、双方、业绩公司和清洗结果只算一次，避免两两比较时重复跑正则。"""
    title = str(doc.get("title") or "")
    doc["_party"] = _party_pair(title)
    doc["_earn"] = _earnings_company(title)
    doc["_policy"] = _policy_subject(doc)
    doc["_subjects"] = _subject_keys(doc)
    doc["_compact"] = _compact(title)
    doc["_norm"] = _norm_title(title)


def _block_keys(doc: dict) -> list[str]:
    """只把可能合并的稿放进同一桶。桶外的两两比较 `_can_merge` 一定是假。"""
    keys: list[str] = []
    action = str(doc.get("action") or "")
    for subject in doc.get("_subjects") or ():
        if action and subject:
            keys.append(f"s:{action}:{subject}")
    policy = str(doc.get("_policy") or "")
    if policy:
        keys.append(f"o:{policy}")
    party = doc.get("_party") or ("", "")
    if party[0] and party[1] and action:
        keys.append("p:" + action + ":" + "|".join(sorted((party[0], party[1]))))
    earn = str(doc.get("_earn") or "")
    if earn:
        keys.append(f"e:{earn}")
    for token in doc.get("tokens") or ():
        text = str(token)
        if len(text) >= 3 and text not in _GENERIC_TOKENS:
            keys.append(f"t:{text}")
    compact = str(doc.get("_compact") or "")
    if len(compact) >= 4:
        keys.append("x:" + compact[:4])
    norm = str(doc.get("_norm") or "")
    if 1 < len(norm) < 4:
        keys.append("g:" + norm)
    elif len(norm) >= 4:
        for start in range(len(norm) - 2):
            keys.append("g:" + norm[start:start + 3])
    if _in_preopen_window(doc["published"]):
        overnight = _overnight_action(doc)
        if overnight:
            for code in doc.get("stocks") or {}:
                keys.append(f"n:s:{overnight}:{code}")
            for concept in doc.get("concepts") or []:
                label = str(concept)
                if _fine_concept(label, label):
                    keys.append(f"n:c:{overnight}:{label}")
    return keys


def _all_pairs(count: int):
    for left in range(count):
        for right in range(left + 1, count):
            yield left, right


def _blocked_pairs(docs: list[dict]):
    buckets: dict[str, list[int]] = defaultdict(list)
    for index, doc in enumerate(docs):
        seen: set[str] = set()
        for key in _block_keys(doc):
            if key in seen:
                continue
            seen.add(key)
            buckets[key].append(index)
    emitted: set[tuple[int, int]] = set()
    for key, indexes in buckets.items():
        if len(indexes) < 2:
            continue
        # 双方、业绩、标题前缀和隔夜锚点不受 6 小时限制。其余桶只比时间窗里的稿。
        limited = not key.startswith(("p:", "e:", "x:", "n:"))
        for left, right in _pairs_in_bucket(docs, indexes, limited=limited):
            if left > right:
                left, right = right, left
            pair = (left, right)
            if pair in emitted:
                continue
            emitted.add(pair)
            yield left, right


def _pairs_in_bucket(docs: list[dict], indexes: list[int], *, limited: bool):
    if not limited:
        for pos, left in enumerate(indexes):
            for right in indexes[pos + 1:]:
                yield left, right
        return
    ordered = sorted(indexes, key=lambda index: docs[index]["published"])
    start = 0
    for pos, right in enumerate(ordered):
        published = docs[right]["published"]
        while start < pos and published - docs[ordered[start]]["published"] > _CLUSTER_WINDOW:
            start += 1
        for left in ordered[start:pos]:
            yield left, right


def _cluster_day(rows: list[dict], now: datetime, *, blocked: bool = True) -> list[dict]:
    docs = [_doc(row) for row in rows]
    docs = [doc for doc in docs if doc is not None]
    if not docs:
        return []
    for doc in docs:
        _prepare_merge_doc(doc)
    parent = list(range(len(docs)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    pairs = _blocked_pairs(docs) if blocked else _all_pairs(len(docs))
    for left, right in pairs:
        if _can_merge(docs[left], docs[right]):
            parent[find(right)] = find(left)
    groups: dict[int, list[dict]] = defaultdict(list)
    for index, doc in enumerate(docs):
        groups[find(index)].append(doc)
    events = []
    for members in groups.values():
        event = _event_from(members, now)
        # 加红稿即使关键词标题还不完整也先留下，后面还有模型和回退标题。
        # 原标题里已经有动作、但清理结果还过不了展示门槛的，也留给模型重写。
        if (
            _cleaned_display_ok(event.get("name"))
            or _first_original_title(_candidate_rows(event))
            or int(event.get("red_count") or 0) > 0
            or _worth_labeling(event)
            or _awaiting_news_title(event)
            or _has_stock_mapping(event)
        ):
            events.append(event)
    return _rank_events(events, now, grade=True)


def _doc(row: dict) -> dict | None:
    title = str(row.get("title") or "").strip()
    summary = _summary_text(row)
    if not title and not summary:
        return None
    source = str(row.get("source") or "")
    red = is_red(source, row.get("level"))
    published = row["published"]
    concepts: list[str] = []
    stocks: dict[str, str] = {}
    for mention in row.get("mentions") or []:
        kind = mention["kind"]
        key = str(mention["key"] or "").strip()
        name = str(mention["name"] or key).strip()
        if not key:
            continue
        if kind == "sector" and _fine_concept(key, name):
            concepts.append(name or key)
        elif kind == "stock":
            stocks[key] = name or key
    lead, action, obj = "", "", ""
    phrase = _strip_report_label(title)
    if phrase and phrase != _compact(title):
        lead, action, obj = _parse_phrase(phrase)
    if not lead or not action:
        lead, action, obj = _parse_phrase(title)
    if not lead or not action:
        lead, action, obj = _parse_phrase(summary)
    shown = title or summary[:80]
    relevance = _relevance(shown, lead, action, concepts, stocks)
    # 加红不依赖涨价、发布这类动词。普通稿仍要主体和动作都在，才不会把闲聊收进榜。
    # 停在单个拉丁字母上的半截标题先留下，好并回完整事件，提及次数才不会丢。
    if relevance < _RELEVANCE_MIN and not red:
        if not (_latin_cutoff_fragment(title) or _informative_policy(title)):
            return None
        relevance = _RELEVANCE_MIN
    entity_text = title
    if summary and not _explicit_entities(title):
        entity_text = f"{title}\n{summary[:160]}"
    entities = _explicit_entities(title)
    if not entities and _subject_is_vague(lead):
        entities = _explicit_entities(summary[:200])
    tokens = _title_tokens(lead, obj, concepts, title or summary[:80], entities)
    return {
        "item_id": row.get("id"),
        "source": source,
        "red": red,
        "published": published,
        "title": title or summary[:80],
        "entity_text": entity_text,
        "text": f"{title}\n{summary}",
        "lead": lead,
        "action": action,
        "object": obj,
        "entities": entities,
        "relevance": relevance,
        "concepts": concepts,
        "stocks": stocks,
        "tokens": tokens,
        "_topic": str(row.get("_topic") or ""),
    }


def _summary_text(row: dict) -> str:
    raw = row.get("extra_json") or ""
    if raw:
        try:
            import json
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = {}
        if isinstance(payload, dict):
            zh = str(payload.get("summary_zh") or "").strip()
            if zh:
                return zh
    return str(row.get("clean_text") or "").strip()


def _is_column_concept(name: str) -> bool:
    """来源栏目不是 A 股概念。俄乌冲突快报只是专栏名。"""
    return "快报" in _compact(name)


def _fine_concept(key: str, name: str) -> bool:
    text = (name or key).strip()
    if _is_column_concept(text) or _is_column_concept(key):
        return False
    if text in BROAD_SECTORS or key in BROAD_SECTORS:
        return False
    return _usable_sector_name(text)


def _parse_phrase(text: str) -> tuple[str, str, str]:
    body = (text or "").strip()
    if not body:
        return "", "", ""
    clauses = [clause.strip() for clause in _CLAUSE.split(body) if clause.strip()]
    chosen = body
    action = ""
    for clause in clauses or [body]:
        for verb in _ACTIONS:
            if verb in clause:
                chosen = clause
                action = verb
                break
        if action:
            break
    if not action:
        for clause in clauses or [body]:
            index, verb = _directive_action(clause)
            if index >= 0 and verb:
                chosen = clause
                action = verb
                break
    if not action:
        for clause in clauses or [body]:
            index, verb = _narrative_action(clause)
            if verb and _subject_ok(clause[:index]):
                chosen = clause
                action = verb
                break
    if not action:
        for clause in clauses or [body]:
            for verb in _GATE_EXTRA_VERBS:
                if verb in clause and _subject_ok(clause[:clause.find(verb)]):
                    chosen = clause
                    action = verb
                    break
            if action:
                break
    if not action:
        return _clean_lead(chosen[:8]), "", ""
    index = chosen.find(action)
    lead = _clean_lead(chosen[:index])
    obj = _CLAUSE.split(chosen[index + len(action):])[0].strip()[:8]
    if len(obj) < 2 or obj in _STOP or obj in BROAD_SECTORS or obj in _GENERIC_TOKENS:
        obj = ""
    return lead, action, obj


def _clean_lead(text: str) -> str:
    lead = (text or "").strip()
    for suffix in _LEAD_SUFFIXES:
        if lead.endswith(suffix) and len(lead) - len(suffix) >= 2:
            lead = lead[:-len(suffix)]
    if len(lead) < 2 or lead in _STOP or lead in BROAD_SECTORS:
        return ""
    return lead[:8]


def _title_tokens(
    lead: str,
    obj: str,
    concepts: list[str],
    title: str,
    entities: list[str] | None = None,
) -> set[str]:
    """聚类词只来自标题里的主体、宾语和标题中出现的细概念。

    正文提及的概念不进聚类词，否则一条快讯带上的苹果、黄金会把橡胶新闻粘进去。
    """
    tokens = set()
    for value in (lead, obj, _subject_core(lead)):
        if (
            value
            and len(value) >= 2
            and value not in _STOP
            and value not in BROAD_SECTORS
            and value not in _GENERIC_TOKENS
        ):
            tokens.add(value)
    for entity in entities or []:
        if entity:
            tokens.add(entity)
    for concept in concepts:
        if concept and concept in (title or "") and concept not in BROAD_SECTORS:
            tokens.add(concept)
    return tokens


def _event_from(members: list[dict], now: datetime) -> dict:
    members = sorted(members, key=lambda item: item["published"])
    first = members[0]["published"]
    latest = members[-1]["published"]
    sources = {item["source"] for item in members if item["source"]}
    age_hours = max(0.0, (now - latest).total_seconds() / 3600)
    decay = math.exp(-math.log(2) * age_hours / _HEAT_HALF_LIFE_HOURS)
    mentions = len(members)
    red_count = sum(1 for item in members if item.get("red"))
    weighted_mentions = (mentions - red_count) + red_count * _RED_MENTION_WEIGHT
    source_count = len(sources) or 1
    name = _keyword_title(members)
    focused = _representative_members(members, name)
    concept_counts: Counter[str] = Counter()
    for item in focused:
        concept_counts.update(dict.fromkeys(item["concepts"], 1))
    focus_titles = [str(item.get("title") or "") for item in focused]
    subjects = _subject_set(name, focused)
    # 概念、分类、方向只看展示标题和同一句原文，不看同簇里另一条稿。
    story = _story_texts(name, focus_titles)
    story_blob = "\n".join(story)
    concepts = _merge_mapped_concepts(
        _relevant_concepts(concept_counts, story, _subject_set(name, [])),
        story_blob,
    )
    rejected = [label for label in concept_counts if label not in concepts and label not in BROAD_SECTORS]
    stocks: dict[str, dict] = {}
    for item in focused:
        for key, stock_name in item["stocks"].items():
            if not _stock_relevant(stock_name, key, focus_titles, subjects, concepts, rejected):
                continue
            hit = stocks.get(key)
            if hit is None:
                hit = {"key": key, "name": stock_name, "mentions": 0, "sources": set()}
                stocks[key] = hit
            hit["mentions"] += 1
            hit["sources"].add(item["source"])
            hit["name"] = stock_name
    mentioned = [
        {
            "key": hit["key"],
            "name": hit["name"],
            "mentions": hit["mentions"],
            "source_count": len(hit["sources"]),
        }
        for hit in stocks.values()
    ]
    mentioned.sort(key=lambda item: (-item["mentions"], -item["source_count"], item["name"]))
    action = _mode([item["action"] for item in focused if item["action"]])
    category = _category_for(story_blob, action)
    direction = _direction_for(story_blob, action)
    for hit in mentioned:
        hit["direction"] = direction
    item_ids = [str(item["item_id"]) for item in members]
    digest_src = ",".join(sorted(item_ids))
    topics = [str(item.get("_topic") or "") for item in members]
    if any(topics):
        # 同一篇晨报拆成的主题仍指向原文，用主题序号把 key 分开。
        digest_src += "#" + ",".join(topics)
    digest = hashlib.sha1(digest_src.encode("utf-8")).hexdigest()[:12]
    seen = first.astimezone(CN_TZ)
    first_seen = seen.strftime("%H:%M") if seen.date() == now.date() else seen.strftime("%m-%d %H:%M")
    headlines: list[str] = []
    for item in members:
        title = str(item["title"] or "").strip()
        if title and title not in headlines:
            headlines.append(title)
        if len(headlines) >= 3:
            break
    headline = headlines[0] if headlines else ""
    event = {
        "key": f"ev_{digest}",
        "name": name,
        "category": category,
        "direction": direction,
        "relevance": max(int(item.get("relevance") or 0) for item in members),
        "concepts": concepts,
        "mapping": [{"name": concept, "kind": "concept", "direction": direction} for concept in concepts],
        "headline": headline,
        "headlines": headlines,
        "subtitle": "",
        "mentions": mentions,
        "red_count": red_count,
        "source_count": len(sources),
        "first_seen": first_seen,
        "updated_at": first_seen,
        "heat": round(weighted_mentions * source_count * decay, 4),
        "mentioned_stocks": mentioned,
        "item_ids": item_ids,
        "_candidates": [
            {
                "source": str(item.get("source") or ""),
                "title": str(item.get("title") or ""),
                # 正文跟着候选一起带出来。要点要写「主标题没提到的事实」，没有正文
                # 就只能把主标题换个说法（实测 20 条要点 20 条都是同义改写）。
                "text": str(item.get("text") or ""),
            }
            for item in members
            if str(item.get("title") or "").strip()
        ],
        "_latest": latest,
        "_first": first,
        "_fp": hashlib.sha1("\n".join(item["title"] for item in members).encode("utf-8")).hexdigest(),
    }
    _annotate_directions(event, story_blob)
    return event


def _rank_events(events: list[dict], now: datetime, *, grade: bool) -> list[dict]:
    """按重要性、映射、新鲜度、热度排序。只有第一次按规则分级时丢掉琐碎小事。"""
    kept = []
    for event in events:
        if grade:
            event["importance"] = _rule_importance(event)
        _score_event(event, now)
        if grade and _drop_minor(event):
            continue
        kept.append(event)
    kept.sort(key=lambda item: (
        -float(item["score"]),
        -_IMPORTANCE_POINTS.get(str(item.get("importance")), 0),
        -float(item["breakdown"]["mapping"]),
        -float(item["breakdown"]["freshness"]),
        -float(item["heat"]),
        str(item["name"]),
    ))
    return kept


def _score_event(event: dict, now: datetime) -> None:
    label = str(event.get("importance") or "一般")
    if label not in _IMPORTANCE_POINTS:
        label = "一般"
        event["importance"] = label
    importance = _IMPORTANCE_POINTS[label]
    concepts = [str(name).strip() for name in (event.get("concepts") or []) if str(name).strip()]
    stocks = [item for item in (event.get("mentioned_stocks") or []) if isinstance(item, dict)]
    mapping = 0
    if concepts:
        mapping += _MAPPING_CONCEPT
    if stocks:
        mapping += _MAPPING_ASSET
    direction = str(event.get("direction") or "")
    if direction in {"利好", "利空"} and (concepts or stocks):
        mapping += _MAPPING_DIRECTION
    age_hours = _freshness_age_hours(event, now)
    freshness = _FRESH_MAX * math.exp(-math.log(2) * age_hours / _HEAT_HALF_LIFE_HOURS)
    try:
        heat = float(event.get("heat") or 0)
    except (TypeError, ValueError):
        heat = 0.0
    heat_bonus = min(_HEAT_BONUS_CAP, max(0.0, heat))
    confirmation = _confirmation_points(event.get("confirmation"))
    event["breakdown"] = {
        "importance": importance,
        "confirmation": confirmation,
        "mapping": mapping,
        "freshness": round(freshness, 4),
        "heat": round(heat_bonus, 4),
    }
    event["score"] = round(importance + confirmation + mapping + freshness + heat_bonus, 4)


def _resync_importance_score(event: dict) -> None:
    """importance 被 _apply_llm 改写成模型级别后，只把这一项和总分对齐。

    写要点（_label_with_llm）会经 _apply_llm 用模型给的级别覆盖 importance，
    而 breakdown/score 还是写要点前按规则级别算的 —— 实测前 20 名里 16 条的
    标签和分数对不上（标签「一般」而 breakdown.importance=130）。
    这里只补 importance 一项和总分：mapping 会随 concepts/stocks 变，
    连它一起重算会让名次跟着「写没写过要点」漂移，打破 _compute 里
    「前 20 必有要点」的保证，所以其余分项保持写要点前的值。
    """
    label = str(event.get("importance") or "一般")
    if label not in _IMPORTANCE_POINTS:
        label = "一般"
        event["importance"] = label
    breakdown = event.get("breakdown")
    if not isinstance(breakdown, dict):
        return
    importance = _IMPORTANCE_POINTS[label]
    if float(breakdown.get("importance") or 0.0) == float(importance):
        return
    breakdown["importance"] = importance
    event["score"] = round(
        importance
        + float(breakdown.get("confirmation") or 0.0)
        + float(breakdown.get("mapping") or 0.0)
        + float(breakdown.get("freshness") or 0.0)
        + float(breakdown.get("heat") or 0.0),
        4,
    )


def _confirmation_mapping(event: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """盘面验证看的个股和概念。用来发现写要点之后才出现的映射。"""
    stocks = tuple(sorted({
        str(item.get("key") or "").strip()
        for item in (event.get("mentioned_stocks") or [])
        if isinstance(item, dict) and str(item.get("key") or "").strip()
    }))
    concepts = tuple(sorted({
        str(name).strip()
        for name in (event.get("concepts") or [])
        if str(name).strip()
    }))
    return stocks, concepts


def _confirm_newly_mapped(
    events: list[dict],
    now: datetime,
    mapped_before: dict[int, tuple[tuple[str, ...], tuple[str, ...]]],
) -> None:
    """榜单定稿后，给这一轮才补上映射、且还没验证的事件做盘面验证。

    请求路径只读快照。补验放在后台快照里，这样新事件在进入本轮榜单时就能判断，
    不用空着强度再等下一轮。已经验证过的不重算，顺序也不改。
    """
    from app.news.market_confirm import load_market_tape, score_confirmation
    from app.news.push import event_is_verified

    pending = []
    for event in events:
        confirm = event.get("confirmation")
        if event_is_verified(confirm if isinstance(confirm, dict) else None):
            continue
        current = _confirmation_mapping(event)
        if current == mapped_before.get(id(event)):
            continue
        if not current[0] and not current[1]:
            continue
        pending.append(event)
    if not pending:
        return
    tape = load_market_tape(now, events)
    upgraded: list[str] = []
    for event in pending:
        event["confirmation"] = score_confirmation(event, tape, now, peers=events)
        _score_event(event, now)
        if event_is_verified(event.get("confirmation")):
            upgraded.append(str(event.get("name") or event.get("key") or ""))
    if upgraded:
        logger.info("热门事件本轮完成盘面验证 %s", "、".join(upgraded))


def _confirmation_points(raw) -> float:
    if not isinstance(raw, dict):
        return 0.0
    try:
        points = float(raw.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0
    if points != points:
        return 0.0
    return round(min(80.0, max(0.0, points)), 4)


def _event_text(event: dict) -> str:
    parts = [str(event.get("name") or ""), str(event.get("headline") or "")]
    parts.extend(str(item) for item in (event.get("headlines") or []))
    return "\n".join(part for part in parts if part)


def _rule_importance(event: dict) -> str:
    text = _event_text(event)
    if _is_major(text):
        label = "重大"
    elif _is_important(text, str(event.get("category") or "")):
        label = "重要"
    else:
        label = "一般"
    if "传闻" in text:
        label = _IMPORTANCE_ORDER[max(0, _IMPORTANCE_ORDER.index(label) - 1)]
    return label


def _price_hike_text(text: str) -> bool:
    return _HIKE_PATTERN.search(text or "") is not None


def _names_company(text: str) -> bool:
    body = _compact(text)
    if _listed_companies(body):
        return True
    return _COMPANY_SUFFIX.search(body) is not None


def _company_price_hike(text: str) -> bool:
    return _price_hike_text(text) and _names_company(text)


def _hike_is_major(text: str) -> bool:
    """公司提价写了幅度，或是再次提价，算重大。没有点名公司的寻常涨价仍是重要。"""
    if not _company_price_hike(text):
        return False
    if _HIKE_PERCENT.search(text):
        return True
    return any(mark in text for mark in _HIKE_REPEAT)


def _is_major(text: str) -> bool:
    if _hike_is_major(text):
        return True
    if any(cue in text for cue in _MAJOR_RATE):
        return True
    if any(org in text for org in _MAJOR_ORGS) and any(act in text for act in _MAJOR_POLICY_ACTS):
        return True
    if any(cue in text for cue in _MAJOR_GEO):
        return True
    if any(name in text for name in _MAJOR_COMMODITIES) and any(verb in text for verb in _PRICE_VERBS):
        return True
    return any(name in text for name in _LEADERS) and any(act in text for act in _LEADER_ACTS)


def _is_important(text: str, category: str) -> bool:
    if any(act in text for act in _IMPORTANT_ACTS) or _price_hike_text(text):
        return True
    if category in _IMPORTANT_CATEGORIES:
        return True
    return any(name in text for name in _LEADERS) and any(verb in text for verb in _PRICE_VERBS)


def _in_preopen_window(published: datetime) -> bool:
    """上一收盘之后到下一交易日 9:30，含周末。15:00 整点仍算当天。"""
    local = published.astimezone(CN_TZ)
    if local.weekday() >= 5:
        return True
    clock = local.time()
    return clock <= dt_time(9, 30) or clock > dt_time(15, 0)


def _overnight_action(doc: dict) -> str:
    """涨价、提价、涨价函、上调价格在隔夜合并里算同一个动作。"""
    if _price_hike_text(str(doc.get("title") or "")):
        return "提价"
    return str(doc.get("action") or "")


def _same_overnight_session(left: dict, right: dict) -> bool:
    if not _in_preopen_window(left["published"]) or not _in_preopen_window(right["published"]):
        return False
    return _news_session_day(left["published"]) == _news_session_day(right["published"])


def _shared_stock_or_fine_concept(left: dict, right: dict) -> bool:
    if set(left.get("stocks") or {}) & set(right.get("stocks") or {}):
        return True
    left_concepts = {
        str(name) for name in (left.get("concepts") or []) if _fine_concept(str(name), str(name))
    }
    right_concepts = {
        str(name) for name in (right.get("concepts") or []) if _fine_concept(str(name), str(name))
    }
    return bool(left_concepts & right_concepts)


def _same_overnight_anchor(left: dict, right: dict) -> bool:
    """同一场开盘里，同一股票或细概念、同一个动作，隔夜也并成一件。"""
    if not _same_overnight_session(left, right):
        return False
    action = _overnight_action(left)
    if not action or action != _overnight_action(right):
        return False
    return _shared_stock_or_fine_concept(left, right)


def _freshness_age_hours(event: dict, now: datetime) -> float:
    """隔夜稿从交易日 9:00 起算，避免周末把周一开盘前的事件埋掉。"""
    first = event.get("_first")
    if not isinstance(first, datetime):
        return float(_HEAT_HALF_LIFE_HOURS)
    if _in_preopen_window(first):
        session = _news_session_day(first)
        anchor = datetime.combine(session, dt_time(9, 0), CN_TZ)
        latest = event.get("_latest")
        if isinstance(latest, datetime) and latest > anchor:
            anchor = latest
        if now <= anchor:
            return 0.0
        return max(0.0, (now - anchor).total_seconds() / 3600)
    return max(0.0, (now - first).total_seconds() / 3600)


def _brief_parts(text: str) -> list[str]:
    body = (text or "").strip()
    if not body:
        return []
    marks = list(_BRIEF_MARK.finditer(body))
    if len(marks) < 2:
        return []
    parts: list[str] = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(body)
        chunk = body[mark.end():end].strip(" \n；;，,。")
        if len(_compact(chunk)) >= 6:
            parts.append(chunk)
    return parts if len(parts) >= 2 else []


def _mention_mapping(mention) -> dict:
    if isinstance(mention, dict):
        return mention
    return {
        "kind": mention["kind"],
        "key": mention["key"],
        "name": mention["name"],
    }


def _mentions_for_part(mentions, part: str) -> list:
    kept = []
    for raw in mentions or []:
        mention = _mention_mapping(raw)
        name = str(mention.get("name") or "")
        key = str(mention.get("key") or "")
        if (name and name in part) or (key and key in part):
            kept.append(mention)
    return kept


def _expand_topic_rows(row: dict) -> list[dict]:
    """研究来源的多主题晨报按条拆开。单条仍用原来的标题。"""
    source = str(row.get("source") or "").strip().lower()
    if source not in _RESEARCH_SOURCES:
        return [row]
    title = str(row.get("title") or "")
    body = _summary_text(row)
    parts = _brief_parts(body)
    if len(parts) < 2:
        parts = _brief_parts(title)
    if len(parts) < 2:
        parts = _brief_parts(f"{title}\n{body}")
    if len(parts) < 2:
        return [row]
    # 标题本身已经能上屏时不拆，避免把一条完整的公司新闻按编号切碎。
    if _research_headline_ok(title):
        return [row]
    mentions = [_mention_mapping(item) for item in (row.get("mentions") or [])]
    expanded = []
    for index, part in enumerate(parts):
        headline = part.strip().split("\n", 1)[0].strip()[:180]
        if len(_compact(headline)) < 6:
            continue
        piece = dict(row)
        piece["title"] = headline
        piece["clean_text"] = part
        piece["mentions"] = _mentions_for_part(mentions, part)
        piece["_topic"] = str(index)
        expanded.append(piece)
    return expanded or [row]


def _drop_minor(event: dict) -> bool:
    """没有映射、只有一条来源的寻常小事不进榜。重大和重要事件留下。"""
    label = str(event.get("importance") or "")
    if label == "琐碎":
        return True
    if label != "一般":
        return False
    if float(event["breakdown"]["mapping"]) > 0:
        return False
    if int(event.get("red_count") or 0) > 0:
        return False
    return int(event.get("mentions") or 0) <= 1 and int(event.get("source_count") or 0) <= 1


def _specific_concepts(counts: Counter[str]) -> list[str]:
    names = [name for name in counts if name not in BROAD_SECTORS]
    names.sort(key=len, reverse=True)
    kept: list[str] = []
    for name in names:
        if any(name != other and name in other for other in kept):
            continue
        kept.append(name)
    kept.sort(key=lambda name: (-counts[name], -len(name), name))
    return kept[:_CONCEPT_LIMIT]


def _display_name_ok(name: object) -> bool:
    text = _compact(str(name or ""))
    return bool(text) and text != "事件"


def _subject_core(lead: str) -> str:
    core = _compact(lead)
    original = core
    changed = True
    while changed and len(core) > 2:
        changed = False
        for suffix in _SUBJECT_TRAIL:
            if core.endswith(suffix) and len(core) - len(suffix) >= 2:
                core = core[:-len(suffix)]
                changed = True
                break
    if not core or core == original or len(core) < 2:
        return ""
    if core in _STOP or core in _GENERIC_TOKENS or core in BROAD_SECTORS:
        return ""
    return core


def _explicit_entities(text: str) -> list[str]:
    if not text:
        return []
    occupied = [False] * len(text)
    spans: list[tuple[int, str]] = []
    for name in _EXPLICIT_SUBJECTS:
        start = 0
        while True:
            index = text.find(name, start)
            if index < 0:
                break
            end = index + len(name)
            if not any(occupied[index:end]):
                spans.append((index, name))
                for pos in range(index, end):
                    occupied[pos] = True
            start = end
    spans.sort()
    seen: list[str] = []
    for _index, name in spans:
        if name not in seen:
            seen.append(name)
    return seen


def _subject_is_vague(lead: str) -> bool:
    text = _normalize_subject(lead) or _subject_text(lead)
    if len(text) < 2:
        return True
    if _explicit_entities(text):
        return False
    if text in _NOT_SUBJECT or text in BROAD_SECTORS or text in _STOP:
        return True
    if text.endswith(("性", "的", "地")):
        return True
    return any(cue in text for cue in _VAGUE_CUES)


@lru_cache(maxsize=8192)
def _norm_title(text: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", _strip_source_prefix(text))


@lru_cache(maxsize=8192)
def _title_grams(text: str) -> frozenset[str]:
    aa = _norm_title(text)
    if not aa:
        return frozenset()
    if len(aa) < 2:
        return frozenset({aa})
    return frozenset(aa[index:index + 2] for index in range(len(aa) - 1))


def _headline_similarity(left: str, right: str) -> float:
    aa = _norm_title(left)
    bb = _norm_title(right)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    ga, gb = _title_grams(left), _title_grams(right)
    union = len(ga | gb)
    if not union:
        return 0.0
    return len(ga & gb) / union


# 公司名左边可以是句首，或这些衔接字。「向东百集团」「与建发股份」里的公司要能切出来。
_COMPANY_GLUE = frozenset("向对就的与和及被把将为给让由从在于")
_COMPANY_HEAD_BLOCK = frozenset({
    "增持", "减持", "持股", "控股", "参股", "本公司", "子公司", "全资",
})


def _company_name_ok(name: str) -> bool:
    """集团、股份结尾，且不是「回购股份」这种把动作当成公司的片段。"""
    if not name.endswith(("集团", "股份")) or len(name) < 4:
        return False
    head = name[:-2]
    if not 2 <= len(head) <= 6 or not all("\u4e00" <= char <= "\u9fff" for char in head):
        return False
    if any(char in _COMPANY_GLUE for char in head):
        return False
    blocked = _STOP | _NOT_SUBJECT | _GENERIC_TOKENS | BROAD_SECTORS | _COMPANY_HEAD_BLOCK
    if head in blocked or name in blocked:
        return False
    for verb in (*_ACTIONS, *_PRICE_NARRATIVE, *_DIRECTIVE_VERBS, *_GATE_EXTRA_VERBS):
        if verb in name:
            return False
    return True


def _listed_companies(text: str) -> list[str]:
    """标题里点名的上市公司。动词前的主语经常不是这家公司，所以单独扫一遍。"""
    body = _compact(text)
    found: list[str] = []
    for suffix in ("集团", "股份"):
        start = 0
        while True:
            index = body.find(suffix, start)
            if index < 0:
                break
            start = index + len(suffix)
            for size in range(2, 7):
                if index < size:
                    break
                head = body[index - size:index]
                if not all("\u4e00" <= char <= "\u9fff" for char in head):
                    break
                name = head + suffix
                if not _company_name_ok(name):
                    continue
                prev = body[index - size - 1] if index - size else ""
                if prev and "\u4e00" <= prev <= "\u9fff" and prev not in _COMPANY_GLUE:
                    continue
                if name not in found:
                    found.append(name)
    return found


def _subject_keys(doc: dict) -> set[str]:
    keys = {entity for entity in (doc.get("entities") or []) if entity}
    lead = str(doc.get("lead") or "")
    if lead and not _subject_is_vague(lead):
        if len(lead) <= 8:
            keys.add(lead)
        core = _subject_core(lead)
        if core:
            keys.add(core)
    # 只扫标题。正文页脚会带出别的公司，不能拿来并事件。
    for name in _listed_companies(str(doc.get("title") or "")):
        keys.add(name)
        core = _subject_core(name)
        if core:
            keys.add(core)
    return keys


def _same_subject_action(left: dict, right: dict) -> bool:
    if not left.get("action") or left.get("action") != right.get("action"):
        return False
    left_keys = left.get("_subjects")
    if left_keys is None:
        left_keys = _subject_keys(left)
    right_keys = right.get("_subjects")
    if right_keys is None:
        right_keys = _subject_keys(right)
    return bool(set(left_keys) & set(right_keys))


_PARTY_PAIR = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9]{2,12})[与和及]([\u4e00-\u9fffA-Za-z0-9]{2,12})"
)


def _party_pair(text: str) -> tuple[str, str]:
    """「赛力斯与宁德时代签署」里的双方。动作紧跟在双方后面才算。"""
    body = _compact(_strip_source_prefix(text))
    body = re.sub(r"([\u4e00-\u9fffA-Za-z0-9]{2,12})[：:]与", r"\1与", body)
    index, verb = _tuple_action(body)
    if index < 0 or not verb:
        return "", ""
    head = _TIME_PREFIX.sub("", body[:index])
    match = _PARTY_PAIR.fullmatch(head)
    if not match:
        return "", ""
    left, right = match.group(1), match.group(2)
    blocked = _NOT_SUBJECT | _STOP | _SOURCE_NAMES | _GENERIC_TOKENS
    if left in blocked or right in blocked or left == right:
        return "", ""
    return left, right


def _same_party_action(left: dict, right: dict) -> bool:
    """A与B做同一件事，和 B与A 是同一事件。动作不同则不并。"""
    if not left.get("action") or left.get("action") != right.get("action"):
        return False
    first = left.get("_party")
    if not isinstance(first, tuple):
        first = _party_pair(str(left.get("title") or ""))
    second = right.get("_party")
    if not isinstance(second, tuple):
        second = _party_pair(str(right.get("title") or ""))
    if not first[0] or not second[0]:
        return False
    return frozenset(first) == frozenset(second)


def _policy_subject(doc: dict) -> str:
    """财政部增量政策和隐债方案是同一件事，即使一条写发布、另一条写出台。"""
    blob = "\n".join([
        str(doc.get("title") or ""),
        str(doc.get("lead") or ""),
        " ".join(str(entity) for entity in (doc.get("entities") or [])),
    ])
    for org in _POLICY_ORGS:
        if org in blob:
            return org
    if "财政" in blob and any(cue in blob for cue in ("增量", "隐债", "化债", "出台", "政策")):
        return "财政部"
    return ""


def _same_policy_subject(left: dict, right: dict) -> bool:
    subject = left.get("_policy")
    if subject is None:
        subject = _policy_subject(left)
    other = right.get("_policy")
    if other is None:
        other = _policy_subject(right)
    return bool(subject) and subject == other


def _earnings_company(text: str) -> str:
    """业绩、预告、扣非前面的公司名。同一家公司的业绩稿并成一件。"""
    body = _compact(_strip_report_comment(_strip_source_prefix(text)))
    found = -1
    for cue in _EARNINGS_CUES:
        index = body.find(cue)
        if index >= 2 and (found < 0 or index < found):
            found = index
    if found < 0:
        return ""
    head = body[:found].strip("的")
    if not 2 <= len(head) <= 12 or head in _NOT_SUBJECT or head in _STOP:
        return ""
    if _tuple_action(head)[0] >= 0 or _narrative_action(head)[0] >= 0:
        return ""
    return head


def _worth_labeling(event: dict) -> bool:
    """原标题里有动作或是完整政策句时，先送给模型，展示门槛放在最后。"""
    for headline in event.get("headlines") or []:
        text = str(headline or "")
        if _informative_policy(text):
            return True
        if _tuple_action(text)[0] >= 0 or _narrative_action(text)[0] >= 0:
            return True
    return False


def _same_company_earnings(left: dict, right: dict) -> bool:
    company = left.get("_earn")
    if company is None:
        company = _earnings_company(str(left.get("title") or ""))
    other = right.get("_earn")
    if other is None:
        other = _earnings_company(str(right.get("title") or ""))
    return bool(company) and company == other


def _can_merge(left: dict, right: dict) -> bool:
    # 同一组双方做同一件事时，合并这一交易日里的全部稿，00:13 和 08:39 也算一件。
    # 同一家公司的业绩预告同样并进这一交易日。其他合并仍看 6 小时。
    if _same_party_action(left, right) or _cutoff_pair(left, right) or _same_company_earnings(left, right):
        return True
    if _same_overnight_anchor(left, right):
        return True
    gap = abs(left["published"] - right["published"])
    if gap > _CLUSTER_WINDOW:
        return False
    if _same_subject_action(left, right):
        return True
    if _same_policy_subject(left, right):
        return True
    if (
        gap <= _SIMILAR_WINDOW
        and _headline_similarity(str(left.get("title") or ""), str(right.get("title") or "")) >= _SIMILARITY_MIN
    ):
        return True
    shared = set(left.get("tokens") or ()) & set(right.get("tokens") or ())
    shared = {token for token in shared if len(token) >= 3 and token not in _GENERIC_TOKENS}
    if not shared:
        return False
    return not (left.get("action") and right.get("action") and left.get("action") != right.get("action"))


def _subject_set(name: str, members: list[dict]) -> set[str]:
    subjects = set(_explicit_entities(name))
    lead, _action, _obj = _split_headline(name)
    if lead and not _subject_is_vague(lead):
        if len(lead) <= 8:
            subjects.add(lead)
        core = _subject_core(lead)
        if core:
            subjects.add(core)
    for item in members:
        for entity in item.get("entities") or []:
            if entity:
                subjects.add(entity)
        item_lead = str(item.get("lead") or "")
        if item_lead and not _subject_is_vague(item_lead):
            if len(item_lead) <= 8:
                subjects.add(item_lead)
            core = _subject_core(item_lead)
            if core:
                subjects.add(core)
    return {subject for subject in subjects if subject and len(subject) >= 2}


def _matches_event(item: dict, name: str, subjects: set[str]) -> bool:
    title = str(item.get("title") or "")
    if not title:
        return False
    if any(len(subject) >= 2 and subject in title for subject in subjects):
        return True
    if name and _headline_similarity(title, name) >= _REPRESENTATIVE_SIMILARITY:
        return True
    norm_name = _norm_title(name)
    for token in item.get("tokens") or ():
        if len(str(token)) >= 3 and str(token) in norm_name:
            return True
    return False


def _representative_members(members: list[dict], name: str) -> list[dict]:
    subjects = _subject_set(name, [])
    chosen = [item for item in members if _matches_event(item, name, subjects)]
    return chosen or list(members)


def _concept_entity_miss(name: str, blob: str) -> bool:
    """概念点了俄乌，正文却只有伊朗，就不能留。同一组里出现任一说法算对上。"""
    for group in _GEO_ENTITY_GROUPS:
        if not any(token in name for token in group):
            continue
        if any(token in blob for token in group):
            continue
        return True
    return False


def _concept_relevant(name: str, blob: str, subjects: set[str]) -> bool:
    if _is_column_concept(name) or _concept_entity_miss(name, blob):
        return False
    if name and name in blob:
        return True
    # 细概念延伸标题里已经出现的词，例如标题写「机器人」、提及是「工业机器人」。
    if _headline_maps_concept(name, blob):
        return True
    checked: list[str] = []
    for subject in subjects:
        checked.append(subject)
        core = _subject_core(subject)
        if core:
            checked.append(core)
    for token in checked:
        if len(token) < 2 or token in _GENERIC_TOKENS or token in _STOP or token in _CONCEPT_GLUE:
            continue
        if token in name:
            return True
        if len(token) <= 8 and name in token:
            return True
    return False


def _headline_maps_concept(name: str, blob: str) -> bool:
    if len(name) < 3 or not blob or name in blob:
        return False
    for size in range(min(len(name) - 1, 6), 1, -1):
        for start in range(0, len(name) - size + 1):
            token = name[start:start + size]
            if token in _CONCEPT_LINK_SKIP:
                continue
            if token in blob:
                return True
    return False


def _relevant_concepts(counts: Counter[str], titles: list[str], subjects: set[str]) -> list[str]:
    blob = "\n".join(titles)
    filtered: Counter[str] = Counter({
        name: count
        for name, count in counts.items()
        if _concept_relevant(str(name), blob, subjects)
    })
    return _specific_concepts(filtered)


def _dictionary_names() -> tuple[str, ...]:
    names: list[str] = []
    for _keys, concepts in _SUBJECT_CONCEPTS:
        for name in concepts:
            if name not in names:
                names.append(name)
    for name in ("基建", "地方债"):
        if name not in names:
            names.append(name)
    return tuple(names)


def _dictionary_concepts(blob: str) -> list[str]:
    """主体在标题里、提及却是空的时候，补上对应的 A 股概念。"""
    found: list[str] = []

    def add(names: tuple[str, ...]) -> None:
        for name in names:
            if name not in found:
                found.append(name)

    for keys, concepts in _SUBJECT_CONCEPTS:
        if all(key in blob for key in keys):
            add(concepts)
    fiscal = "财政部" in blob or (
        "财政" in blob and any(cue in blob for cue in ("增量", "隐债", "化债", "出台", "政策"))
    )
    if fiscal and any(cue in blob for cue in ("增量", "隐债", "化债", "出台", "政策")):
        add(("基建", "地方债"))
    return found


def _concept_grounded(name: str, text: str) -> bool:
    """词典概念必须写在这条标题里，或它的触发词就在这条标题里。"""
    blob = text or ""
    if name and name in blob:
        return True
    for keys, concepts in _SUBJECT_CONCEPTS:
        if name in concepts and all(key in blob for key in keys):
            return True
    if name in {"基建", "地方债"}:
        fiscal = "财政部" in blob or (
            "财政" in blob and any(cue in blob for cue in ("增量", "隐债", "化债", "出台", "政策"))
        )
        return bool(
            fiscal and any(cue in blob for cue in ("增量", "隐债", "化债", "出台", "政策"))
        )
    return False


def _merge_mapped_concepts(concepts: list[str], blob: str) -> list[str]:
    merged: list[str] = []
    for name in [*_dictionary_concepts(blob), *concepts]:
        if name and not _is_column_concept(name) and name not in merged:
            merged.append(name)
        if len(merged) >= _CONCEPT_LIMIT:
            break
    return merged


def _stock_relevant(
    name: str,
    key: str,
    titles: list[str],
    subjects: set[str],
    kept_concepts: list[str],
    rejected: list[str],
) -> bool:
    """代表成员上的个股默认留下。名字只贴着被丢掉的概念时才剔除。"""
    label = str(name or "").strip()
    blob = "\n".join(titles)
    if label and label in blob:
        return True
    if key and str(key) in blob:
        return True
    tokens: list[str] = list(kept_concepts)
    for subject in subjects:
        tokens.append(subject)
        core = _subject_core(subject)
        if core:
            tokens.append(core)
    if label and any(len(token) >= 2 and token not in _GENERIC_TOKENS and token in label for token in tokens):
        return True
    tied_to_rejected = bool(label) and any(
        len(concept) >= 2 and (concept in label or label in concept) for concept in rejected
    )
    return not tied_to_rejected


def _ends_split_verb(text: str) -> bool:
    if not text or any(text.endswith(verb) for verb in _VERB_PAIRS):
        return False
    return any(text.endswith(verb[0]) for verb in _VERB_PAIRS)


def _ends_mid_number(text: str) -> bool:
    return bool(text) and text[-1].isdigit()


def _verb_spans(text: str) -> list[tuple[int, str]]:
    """按出现位置列出动词。同一处只留更长的那个。"""
    found: list[tuple[int, str]] = []
    for verb in (*_ACTIONS, *_PRICE_NARRATIVE, *_DIRECTIVE_VERBS, "重申"):
        start = 0
        while True:
            index = text.find(verb, start)
            if index < 0:
                break
            found.append((index, verb))
            start = index + len(verb)
    found.sort(key=lambda item: (item[0], -len(item[1])))
    kept: list[tuple[int, str]] = []
    occupied = -1
    for index, verb in found:
        if index < occupied:
            continue
        kept.append((index, verb))
        occupied = index + len(verb)
    return kept


def _misordered_title(text: str) -> bool:
    """句首动词没有主体，后面又接上另一组主体和动作。重申…白宫…签署就是这种。"""
    body = re.sub(r"[《》「」]", "", _compact(text))
    spans = _verb_spans(body)
    if len(spans) < 2:
        return False
    index, verb = spans[0]
    if index > 0 and _subject_ok(body[:index]):
        return False
    for later, _later_verb in spans[1:]:
        lead = body[index + len(verb):later]
        if _subject_ok(lead):
            return True
    return False


def _watch_or_list_fragment(text: str) -> bool:
    """持续关注开头，或带 1）、① 的列表残句，不是事件。"""
    body = _compact(text)
    if not body:
        return False
    if body.startswith("持续关注"):
        return True
    return _LIST_MARKER.search(body) is not None


def _display_clause_ok(text: str) -> bool:
    """整句可以比 20 字长，但不能停在半个动词、半个数字或半个词上。"""
    body = _compact(text)
    if (
        _title_incoherent(body)
        or _has_file_debris(body)
        or _watch_or_list_fragment(body)
        or _misordered_title(body)
        or _surface_block_reason(body)
    ):
        return False
    if not body or body == "事件" or len(body) < 4 or len(body) > _TITLE_HARD:
        return False
    if any(char in body for char in "【】[]"):
        return False
    if _starts_fragment(body) or _ends_abruptly(body) or _ends_split_verb(body) or _ends_mid_number(body):
        return False
    lead, action, obj = _split_headline(body)
    if not action or not _subject_ok(lead):
        return False
    consumed = len(lead) + len(action) + len(obj)
    if body[consumed:].strip("，。！？、；："):
        return False
    return _display_gate_reason(body) == ""


def _colon_subject(head: str) -> bool:
    """冒号左边是公司或机构简称，不是已经剥过的媒体名。"""
    if not head or len(head) > 8 or head in _SOURCE_NAMES:
        return False
    if _tuple_action(head)[0] >= 0 or _narrative_action(head)[0] >= 0:
        return False
    if head in _NOT_SUBJECT or head in _STOP or head in BROAD_SECTORS or head in _GENERIC_TOKENS:
        return False
    return bool(re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9]{2,8}", head))


def _join_colon_head(head: str, nxt: str) -> str:
    """公司名不是来源。后面以公司/本公司起笔就换上这家公司，与/和则把双方接上。"""
    if not _colon_subject(head) or not nxt:
        return ""
    for generic in ("本公司", "公司"):
        if nxt.startswith(generic):
            return head + nxt[len(generic):]
    if nxt.startswith("与") and not nxt.startswith("与此"):
        return head + nxt
    if nxt.startswith("和") and not _starts_fragment(nxt):
        return head + nxt
    if _starts_fragment(nxt):
        return ""
    if _tuple_action(nxt)[0] == 0 or _narrative_action(nxt)[0] == 0:
        return ""
    if _tuple_action(nxt)[0] > 0 or _narrative_action(nxt)[0] > 0:
        return head + nxt
    return ""


def _stitch_subject_verb(body: str) -> str:
    """「IEA：下调原油供需预期」的冒号把主体和动词拆开了，先接回去。

    已知媒体前缀已经剥过。公司名后面的冒号同样接回去，不能把公司名当成来源丢掉。
    """
    parts = [part.strip() for part in _CLAUSE.split(body) if part.strip()]
    if len(parts) < 2:
        return body
    head, nxt = parts[0], parts[1]
    joined = _join_colon_head(head, nxt)
    if not joined:
        if len(head) > 8 or _tuple_action(head)[0] >= 0 or _narrative_action(head)[0] >= 0:
            return body
        if _tuple_action(nxt)[0] != 0 and _narrative_action(nxt)[0] != 0:
            return body
        joined = head + nxt
    parts[0] = joined
    del parts[1]
    return parts[0] if len(parts) == 1 else parts[0] + "，" + "，".join(parts[1:])


def _cut_ellipsis_tail(text: str) -> str:
    """省略号后面的「持续关注」是点评尾巴，事件停在省略号前。"""
    match = re.search(r"\.{2,}|…+", text or "")
    if not match:
        return text
    head = text[:match.start()].strip("，。！？、；：")
    return head or text


def _strip_report_comment(text: str) -> str:
    """去掉括号里的补充，以及「预告点评」里的点评。机构点评美联储降息这种留下。"""
    body = _PAREN_NOTE.sub("", text or "")
    return _REPORT_DIANPING.sub("", body)


def _prepare_headline(text: str) -> str:
    body = _cut_ellipsis_tail(_strip_source_prefix(text))
    body = _strip_report_comment(body)
    body = _first_sentence(_stitch_subject_verb(body))
    return _attach_following_object(_cut_glued_sentence(body))


def _cleaned_headline(text: str) -> str:
    """去掉来源前缀和钟点，停在第一句，过长时再停在分句内部的自然断点。"""
    body = _prepare_headline(text)
    if not body or body == "事件":
        return ""
    signed = _signing_title(body)
    parts = [part.strip() for part in _CLAUSE.split(body) if part.strip()] or [body]
    for part in parts:
        cleaned = _TIME_PREFIX.sub("", _drop_leading_fragment(part)).strip("，。！？、；：\"'“”")
        if cleaned and cleaned != "事件":
            clipped = _clip_at_boundary(cleaned)
            if clipped and clipped != "事件":
                return _prefer_signed(clipped, body, signed)
    return signed


def _core_event_clause(text: str) -> str:
    """超过 20 字、又用叠加或接续粘了原因时，只留后面的核心事件。"""
    body = _compact(text)
    if len(body) <= _TITLE_LIMIT:
        return body
    cut_at = -1
    span = 0
    for glue in _RUNON_GLUE:
        index = body.rfind(glue)
        if index > cut_at:
            cut_at = index
            span = len(glue)
    if cut_at < 0:
        return body
    core = body[cut_at + span:].lstrip("，。！？、；：")
    if len(core) > _TITLE_LIMIT:
        for tail in _CAUSE_TAILS:
            index = core.find(tail)
            if not 0 < index <= 8:
                continue
            rest = core[index + len(tail):].lstrip("的")
            if 6 <= len(rest) <= _TITLE_LIMIT:
                core = rest
                break
    if core and core != body and 6 <= len(core) <= _TITLE_LIMIT and not _starts_fragment(core):
        return core
    return body


def _is_ascii_alnum(char: str) -> bool:
    return bool(char) and char.isascii() and char.isalnum()


def _retreat_open_alnum(text: str, window: str) -> str:
    """截断点落在字母或数字中间时，整串退回，不留下 Vicor对V 这种半个词。"""
    if not window or not text.startswith(window) or len(window) >= len(text):
        return window
    if not (_is_ascii_alnum(window[-1]) and _is_ascii_alnum(text[len(window)])):
        return window
    while window and _is_ascii_alnum(window[-1]):
        window = window[:-1]
    return window


def _clip_at_boundary(text: str) -> str:
    if (
        _title_incoherent(text)
        or _watch_or_list_fragment(text)
        or _misordered_title(text)
        or _surface_block_reason(text)
    ):
        return ""
    core = _core_event_clause(text)
    if core and core != _compact(text):
        text = core
    if _has_file_debris(text):
        text = _strip_report_label(text)
    if not text or text == "事件" or _surface_block_reason(text):
        return ""
    if _display_clause_ok(text):
        return _gated_text(text)
    signed = _signing_title(text)
    if signed and _gated_text(signed):
        return _gated_text(signed)
    # 没超过上限就整句留下。残词（停在「政」上）仍要削掉，那不是按字数截断。
    # 超长才按上限截，并且不能切开连续的字母或数字。
    if len(text) <= _TITLE_HARD:
        window = text
        if _ends_abruptly(window) or _ends_split_verb(window) or _ends_mid_number(window):
            while window and (
                _ends_abruptly(window)
                or _ends_split_verb(window)
                or _ends_mid_number(window)
                or window[-1] in "的和与及"
            ):
                window = window[:-1]
    else:
        window = _retreat_open_alnum(text, text[:_TITLE_HARD])
        if _ends_mid_number(window):
            while window and window[-1].isdigit():
                window = window[:-1]
            window = _retreat_open_alnum(text, window)
        if len(text) > len(window) and _ends_split_verb(window):
            window = window[:-1]
        cut = -1
        for sep in ("以及", "并且", "同时", "和", "与", "及"):
            index = window.rfind(sep)
            if index >= 4:
                cut = max(cut, index)
        if cut >= 4:
            window = window[:cut]
        while window and (
            _ends_abruptly(window)
            or _ends_split_verb(window)
            or _ends_mid_number(window)
            or window[-1] in "的和与及"
        ):
            window = window[:-1]
        window = _retreat_open_alnum(text, window)
    if _display_clause_ok(window) and not _drops_known_action(window, text):
        return _gated_text(window)
    repaired = _repair_headline(text)
    if (
        repaired
        and not _drops_known_action(repaired, text)
        and not _is_harmful_cutoff(repaired, text)
        and (_display_clause_ok(repaired) or _title_ok(repaired))
        and _gated_text(repaired)
    ):
        return _gated_text(repaired)
    if (
        len(window) >= 4
        and window != "事件"
        and not _is_section_label(window)
        and not _ends_abruptly(window)
        and not _ends_split_verb(window)
        and not _ends_mid_number(window)
        and not _surface_block_reason(window)
        and not _drops_known_action(window, text)
        and _gated_text(window)
    ):
        return _gated_text(window)
    return ""


def _ensure_subject(title: str, source: str) -> str:
    text = _compact(title)
    if not _display_name_ok(text):
        return ""
    lead, action, _obj = _split_headline(text)
    if action and lead and not _subject_is_vague(lead):
        return text
    entities = _explicit_entities(source)
    if not entities:
        return text
    if lead and any(entity in lead for entity in entities) and not _subject_is_vague(lead):
        return text
    attached = _attach_entity(text, entities[0])
    return attached if _display_name_ok(attached) else text


def _attach_entity(title: str, entity: str) -> str:
    if not entity:
        return title
    lead, action, obj = _split_headline(title)
    if entity in title and lead and not _subject_is_vague(lead):
        return title
    prefixed = title if entity in title else f"{entity}{title}"
    prefixed_lead, _prefixed_action, _prefixed_obj = _split_headline(prefixed)
    if (
        prefixed_lead
        and not _subject_is_vague(prefixed_lead)
        and (_display_clause_ok(prefixed) or (len(prefixed) <= _TITLE_LIMIT and _title_ok(prefixed)))
    ):
        return prefixed
    if action:
        fitted = _fit_object(f"{entity}{action}", obj)
        if _display_clause_ok(fitted) or _title_ok(fitted):
            return fitted
    if len(prefixed) <= _TITLE_LIMIT and _title_ok(prefixed):
        return prefixed
    return title


def _keyword_title(members: list[dict]) -> str:
    """用代表标题的第一分句，不再把各条的主体、动作、宾语拼在一起。

    合格分句优先。没有合格分句时，退回去掉来源前缀、按分句截断的原标题。
    仍然没有可读文本时返回空字符串，调用方把这条事件丢掉，不再显示「事件」。
    """
    clauses: list[str] = []
    for item in members:
        clause = _representative_clause(str(item.get("title") or ""))
        if clause and clause != "事件":
            clauses.append(clause)
    blob = "\n".join(
        str(item.get("entity_text") or item.get("title") or "")
        for item in members
    )
    if clauses:
        counts = Counter(clauses)
        best = max(counts.values())
        for clause in clauses:
            if counts[clause] != best:
                continue
            ensured = _ensure_subject(clause, blob)
            if _cleaned_display_ok(ensured):
                return ensured
    for item in members:
        cleaned = _cleaned_headline(str(item.get("title") or ""))
        if not cleaned:
            continue
        ensured = _ensure_subject(cleaned, blob)
        if _cleaned_display_ok(ensured):
            return ensured
    return ""


def _is_noise(title: str) -> bool:
    text = re.sub(r"\s+", "", (title or "").strip())
    if not text or text in _NOISE_EXACT or len(text) < 4:
        return True
    if sum(1 for char in text if "\u4e00" <= char <= "\u9fff") < 2:
        return True
    return any(hint in text for hint in _NOISE_HINTS)


def _relevance(title: str, lead: str, action: str, concepts: list[str], stocks: dict) -> int:
    """有主体也有动作才算一条能进榜的资讯。图片、闲聊和广告是 0。"""
    if _is_noise(title) or not lead or not action:
        return 0
    score = 3
    if concepts:
        score += 1
    if stocks:
        score += 1
    return score


def _category_for(text: str, action: str) -> str:
    blob = text or ""
    for name, cues in _CATEGORY_CUES:
        if any(cue in blob for cue in cues):
            return name
    return _ACTION_CATEGORY.get(action, "科技与产业")


def _category_allowed(category: str, text: str, action: str) -> bool:
    """模型给的分类必须和这条标题对得上，不能把别的簇的海外市场贴过来。"""
    if category not in CATEGORY_NAMES:
        return False
    if category == _category_for(text, action):
        return True
    for name, cues in _CATEGORY_CUES:
        if name == category:
            return any(cue in (text or "") for cue in cues)
    return False


# 负面动作先于「订单」「发布」。削减订单是利空，不是因为出现了订单就算利好。
_BEAR_VERBS = (
    "削减", "砸单", "砸价", "下调", "取消", "暴跌", "减产",
    "砍单", "下修", "大跌", "下跌", "跳水", "走弱", "走低", "降价",
    "停产", "召回", "制裁", "立案", "收紧", "加息",
    "关税", "冲突", "战争",
)
_BULL_VERBS = (
    "上调", "涨价", "提价", "上涨", "大涨", "暴涨", "飙升", "走强", "走高",
    "回购", "获批", "降息", "降准", "补贴", "中标",
    "签约", "签署", "签订", "追加", "增长", "新增",
)


def _earliest_verb(text: str, verbs: tuple[str, ...]) -> int:
    found = -1
    for verb in verbs:
        index = text.find(verb)
        if index >= 0 and (found < 0 or index < found):
            found = index
    return found


def _story_texts(name: str, titles: list[str]) -> list[str]:
    """只留和展示标题是同一句的原文。同簇里另一条稿不参与方向和概念。"""
    anchor = _compact(name)
    kept: list[str] = []
    if not anchor:
        for title in titles:
            compact = _compact(str(title or ""))
            if compact and compact not in kept:
                kept.append(compact)
        return kept
    kept.append(anchor)
    for title in titles:
        compact = _compact(str(title or ""))
        if not compact or compact in kept:
            continue
        if (
            anchor in compact
            or compact in anchor
            or _headline_similarity(anchor, compact) >= _SIMILARITY_MIN
        ):
            kept.append(compact)
    return kept


def _direction_signal(text: str, action: str) -> str:
    """有明确动作时返回利好或利空。对不上则返回空，调用方再默认利好。"""
    blob = text or ""
    bear_at = _earliest_verb(blob, _BEAR_VERBS)
    bull_at = _earliest_verb(blob, _BULL_VERBS)
    if bear_at >= 0 and (bull_at < 0 or bear_at <= bull_at):
        return "利空"
    if bull_at >= 0:
        return "利好"
    if any(cue in blob for cue in _BEAR_CUES):
        return "利空"
    if any(cue in blob for cue in _BULL_CUES):
        return "利好"
    if action in {"降价", "下调", "立案", "停产", "召回", "制裁", "收紧", "加息"}:
        return "利空"
    return ""


def _direction_for(text: str, action: str) -> str:
    return _direction_signal(text, action) or "利好"


def _explicit_target_direction(name: str, text: str) -> str:
    """标题写明利好或利空某个名字时才分开标。砸价本身不自动利好下游。"""
    label = _compact(str(name or ""))
    body = _compact(text or "")
    if len(label) < 2 or label not in body:
        return ""
    for mark in ("利好", "受益"):
        if (
            f"{mark}{label}" in body
            or f"{mark}下游{label}" in body
            or f"{mark}下游的{label}" in body
            or f"{label}{mark}" in body
            or f"{label}将{mark}" in body
        ):
            return "利好"
    for mark in ("利空", "拖累"):
        if f"{mark}{label}" in body or f"{label}{mark}" in body or f"{label}将{mark}" in body:
            return "利空"
    return ""


def _annotate_directions(event: dict, text: str) -> None:
    direction = str(event.get("direction") or "")
    if direction not in {"利好", "利空"}:
        direction = "利好"
        event["direction"] = direction
    for item in event.get("mapping") or []:
        if not isinstance(item, dict):
            continue
        explicit = _explicit_target_direction(str(item.get("name") or ""), text)
        item["direction"] = explicit or direction
    for stock in event.get("mentioned_stocks") or []:
        if not isinstance(stock, dict):
            continue
        explicit = _explicit_target_direction(str(stock.get("name") or ""), text)
        stock["direction"] = explicit or direction


def _mode(values: list[str]) -> str:
    if not values:
        return ""
    return Counter(values).most_common(1)[0][0]


@lru_cache(maxsize=8192)
def _compact(text: str, *, keep_quotes: bool = False) -> str:
    body = re.sub(r"\s+", "", (text or "").strip())
    marks = "，。！？、；："
    if not keep_quotes:
        marks += "\"'“”"
    return body.strip(marks)


def _title_incoherent(text: str) -> bool:
    """「在前」紧挨职务才是拆词，例如「在前法官」。在前三季度、在前述不是。"""
    return bool(_BROKEN_FORMER.search(_compact(text)))


def _file_debris_reason(text: str) -> str:
    body = _compact(text)
    if "#" in body or "＃" in body or _FILE_EXT.search(body):
        return "含文件标记"
    if _FILE_DATE.search(body):
        return "含日期戳"
    return ""


def _has_file_debris(text: str) -> bool:
    return bool(_file_debris_reason(text))


def _surface_block_reason(text: str) -> str:
    """研报标签、没写完的数量、没有主体的产品名、整句只剩券商组名。"""
    body = _compact(text)
    if not body:
        return ""
    if "观点更新" in body or _TOPIC_JOIN.search(body):
        return "研报标签"
    if "对比" in body and re.search(r"[&＆]", body):
        return "研报标签"
    if body.endswith("对比") and _tuple_action(body)[0] < 0 and _narrative_action(body)[0] < 0:
        return "不是具体事件"
    if _MID_PHRASE_CUT.search(body):
        return "结尾不完整"
    if _SUBJECTLESS_PRODUCT.fullmatch(body):
        return "缺少主体"
    if _BROKER_TEAM_TAIL.search(body):
        return "券商组名"
    return ""


def _looks_like_report_file(text: str) -> bool:
    body = _FILE_EXT.sub("", _compact(text))
    if not body:
        return False
    if _FILE_DATE.search(body):
        return True
    return any(body.endswith(kind) for kind in _REPORT_KINDS)


def _strip_report_label(text: str, *, keep_quotes: bool = False) -> str:
    """去掉研报文件名。只删连字符后或 # 前的日期，正文里的央行日期留下。"""
    body = _FILE_EXT.sub("", _compact(text, keep_quotes=keep_quotes))
    parts = re.split(r"[#＃]", body)
    if len(parts) >= 2 and _looks_like_report_file(parts[0]):
        body = _compact("".join(parts[1:]), keep_quotes=keep_quotes)
    elif len(parts) >= 2:
        body = _compact("".join(parts), keep_quotes=keep_quotes)
    body = _REPORT_PREFIX.sub("", body)
    body = _FILE_DATE.sub("", body)
    return body.strip("-_—–#＃")


@lru_cache(maxsize=8192)
def _strip_source_prefix(text: str, *, keep_quotes: bool = False) -> str:
    body = _compact(text, keep_quotes=keep_quotes)
    body = _CLOCK_PREFIX.sub("", body)
    body = _MINUTE_BEFORE_BRACKET.sub("", body)
    changed = True
    while body and changed:
        changed = False
        for pattern in (
            _SOURCE_BRACKET, _SOURCE_DATELINE, _SOURCE_LABEL,
            _BROKER_NOTE, _BROKER_TEAM_LEAD, _LEADING_ECONOMIC_DAILY,
        ):
            updated = pattern.sub("", body, count=1)
            if updated != body:
                body = updated.lstrip("，。！？、；：")
                changed = True
    body = _BROKER_TEAM_TAIL.sub("", body)
    body = _REUTERS_TAIL.sub("", body)
    body = _BROKEN_BRACKET.sub("", body)
    body = _BRACKET_PAIR.sub("", body)
    body = _STRAY_BRACKET.sub("", body)
    body = _LEFTOVER_MINUTE.sub("", body)
    body = _TRAILING_ELLIPSIS.sub("", body)
    body = _strip_report_label(body, keep_quotes=keep_quotes)
    marks = "，。！？、；："
    if not keep_quotes:
        marks += "\"'“”"
    return body.strip(marks)


def _first_sentence(text: str) -> str:
    body = (text or "").strip()
    if not body:
        return ""
    parts = [part.strip() for part in _SENTENCE_END.split(body) if part.strip()]
    return parts[0] if parts else body


def _starts_fragment(text: str) -> bool:
    if not text:
        return False
    if _FRAGMENT_PREFIX.match(text):
        return True
    return text.startswith("与")


def _drop_leading_fragment(text: str) -> str:
    body = text
    while body:
        match = _FRAGMENT_PREFIX.match(body)
        if match:
            body = body[match.end():]
            continue
        if body.startswith("与"):
            body = body[1:]
            continue
        break
    return body


def _ends_abruptly(text: str) -> bool:
    if not text:
        return True
    last = text[-1]
    if last in _DANGLING_FINAL:
        return True
    allowed = _CLOSED_TAILS.get(last)
    return allowed is not None and text[-2:] not in allowed


def _subject_text(before: str) -> str:
    lead = _compact(before)
    changed = True
    while changed and lead:
        changed = False
        for suffix in _LEAD_SUFFIXES:
            if lead.endswith(suffix) and len(lead) - len(suffix) >= 2:
                lead = lead[:-len(suffix)]
                changed = True
                break
    return lead


def _subject_ok(before: str) -> bool:
    lead = _subject_text(before)
    if len(lead) < 2 or lead in _NOT_SUBJECT or lead in BROAD_SECTORS:
        return False
    if _starts_fragment(lead) or lead.endswith(("性", "的", "地", "超")):
        return False
    if lead.endswith(("针对", "对于", "关于")):
        return False
    if any(word in lead for word in _PRICE_NARRATIVE):
        return False
    return _tuple_action(lead)[0] < 0


def _concrete_subject(before: str) -> bool:
    """同比、数据显示不是主体。国别和公司名可以。"""
    text = _subject_text(before)
    if len(text) < 2 or text in _NOT_SUBJECT or text in _GENERIC_TOKENS:
        return False
    core = text
    for prefix in _STAT_PREFIXES:
        if core.startswith(prefix):
            core = core[len(prefix):].lstrip("的")
            break
    if len(core) < 2 or core in {"显示", "表明", "称"}:
        return False
    return core not in _NOT_SUBJECT and core not in _GENERIC_TOKENS


def _normalize_subject(before: str) -> str:
    lead = _drop_leading_fragment(_subject_text(before))
    lead = _TIME_PREFIX.sub("", lead)
    for suffix in ("针对", "对于", "关于"):
        if lead.endswith(suffix) and len(lead) - len(suffix) >= 2:
            lead = lead[:-len(suffix)]
    return lead


def _qualified_directive_subject(before: str) -> bool:
    """布局、建议、推荐只有跟在监管机构或公司名后面才算动作。"""
    text = _subject_text(before)
    if not text or not _subject_ok(text):
        return False
    if any(org in text for org in _ORG_SUBJECTS) or any(name in text for name in _LEADERS):
        return True
    if text in BROAD_SECTORS or any(word in text for word in _VAGUE_ACTORS):
        return False
    if text in _NOT_SUBJECT or text in _GENERIC_TOKENS:
        return False
    return len(text) >= 3 and bool(re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9]+", text))


def _directive_action(text: str) -> tuple[int, str]:
    """机构或公司名后面的布局、建议、推荐。句首的建议关注、推荐布局不是。"""
    found: tuple[int, str] | None = None
    for verb in _DIRECTIVE_VERBS:
        start = 0
        while True:
            index = text.find(verb, start)
            if index < 0:
                break
            if index >= 2 and _qualified_directive_subject(text[:index]):
                if found is None or index < found[0] or (index == found[0] and len(verb) > len(found[1])):
                    found = (index, verb)
                break
            start = index + len(verb)
    return found if found else (-1, "")


def _tuple_action(text: str) -> tuple[int, str]:
    """取最靠前的动作。追加回购要落在追加上，宾语才是回购，而不是空的。"""
    found: tuple[int, str] | None = None
    for verb in _ACTIONS:
        index = text.find(verb)
        if index < 0:
            continue
        if found is None or index < found[0] or (index == found[0] and len(verb) > len(found[1])):
            found = (index, verb)
    return found if found else (-1, "")


def _narrative_action(text: str) -> tuple[int, str]:
    found: tuple[int, str] | None = None
    for verb in _PRICE_NARRATIVE:
        index = text.find(verb)
        if index < 0:
            continue
        if found is None or index < found[0] or (index == found[0] and len(verb) > len(found[1])):
            found = (index, verb)
    return found if found else (-1, "")


def _split_headline(text: str) -> tuple[str, str, str]:
    """选出主体说得通的谓语。叙述后面再接发布、出台这类动作时，宾语停在那之前。"""
    options: list[tuple[int, int, str, str]] = []
    for index, verb in (_tuple_action(text), _narrative_action(text), _directive_action(text)):
        if index < 0 or not verb or not _subject_ok(text[:index]):
            continue
        options.append((index, -len(verb), verb, text[:index]))
    if not options:
        return "", "", ""
    options.sort()
    index, _neg, verb, lead = options[0]
    rest = text[index + len(verb):]
    obj = rest
    if verb in _PRICE_NARRATIVE:
        later, later_verb = _tuple_action(rest)
        if later > 0 and later_verb in _GLUE_FOLLOWUPS:
            obj = rest[:later]
    return lead, verb, obj.strip("，。！？、；：")


def _clause_continues_object(text: str) -> bool:
    """逗号后面没有新的主体和动作，就是上一句的宾语，不是另一件事。"""
    body = _compact(text)
    if not body or _starts_fragment(body) or _watch_or_list_fragment(body):
        return False
    for glue in _RUNON_GLUE:
        if body.startswith(glue):
            return False
    for finder in (_tuple_action, _narrative_action, _directive_action):
        index, verb = finder(body)
        if not verb:
            continue
        if finder is _directive_action or _subject_ok(body[:index]):
            return False
    return True


def _attach_following_object(body: str) -> str:
    """不超过 28 字时，不要把「定调，固态电池…」收成只有「定调」。"""
    parts = [part.strip() for part in _CLAUSE.split(body) if part.strip()]
    if len(parts) < 2 or len(_compact(body)) > _TITLE_HARD:
        return body
    lead, action, obj = _split_headline(parts[0])
    if not action or obj or not _subject_ok(lead):
        return body
    extra: list[str] = []
    for part in parts[1:]:
        if not _clause_continues_object(part):
            break
        extra.append(part)
    if not extra:
        return body
    joined = parts[0] + "".join(extra)
    if len(joined) > _TITLE_HARD:
        return body
    if _display_clause_ok(joined) or _title_ok(joined):
        return joined
    return body


def _ends_mid_clause(text: str) -> bool:
    """停在「集体」或半个词上，说明这一句还没写完。"""
    body = _compact(text)
    if not body:
        return True
    if any(body.endswith(tail) for tail in _FALLBACK_BAD_TAILS):
        return True
    return _ends_abruptly(body)


def _cut_glued_sentence(text: str) -> str:
    """价格叙述后面又接下一句时，停在分句边界，不能把「集体涨价」切成「集体」。"""
    body = _compact(text)
    if not body:
        return ""
    index, verb = _narrative_action(body)
    if index < 0 or not _subject_ok(body[:index]):
        return body
    rest = body[index + len(verb):]
    later, later_verb = _tuple_action(rest)
    if later <= 0 or later_verb not in _UNGLUE_VERBS:
        return body
    head = body[:index + len(verb) + later].rstrip("，。！？、；：")
    gap = body[index + len(verb):index + len(verb) + later]
    boundary = max((gap.rfind(char) for char in "，。！？、；"), default=-1)
    if boundary >= 0:
        return body[:index + len(verb) + boundary].rstrip("，。！？、；：")
    if _ends_mid_clause(head):
        return body[:index + len(verb) + later + len(later_verb)]
    return head


def _title_ok(title: str) -> bool:
    """完整、有主体和动作、不超过 20 字，且不是半句话或词中间截断。"""
    text = _compact(title)
    if (
        _title_incoherent(text)
        or _has_file_debris(text)
        or _watch_or_list_fragment(text)
        or _misordered_title(text)
        or _surface_block_reason(text)
    ):
        return False
    if not text or len(text) > _TITLE_LIMIT or text in BROAD_SECTORS:
        return False
    if _starts_fragment(text) or _ends_abruptly(text):
        return False
    if re.search(r"超(?!过|预|标|额|出|市)", text):
        return False
    lead, action, obj = _split_headline(text)
    if not action or not _subject_ok(lead):
        return False
    tail = text[len(lead) + len(action) + len(obj):].strip("，。！？、；：")
    return not tail


def _fit_object(base: str, obj: str) -> str:
    obj = obj.strip("，。！？、；：")
    if not obj:
        return base
    full = base + obj
    if len(full) <= _TITLE_HARD and not _ends_abruptly(obj) and not _ends_mid_number(full):
        return full
    # 宾语本身是半个词（例如停在「政」）时整段丢掉，不要只削掉最后一个字。
    if not _ends_abruptly(obj):
        room = _TITLE_HARD - len(base)
        chunk = obj[:room] if room > 0 else ""
        while chunk and (
            _ends_abruptly(base + chunk)
            or _ends_split_verb(base + chunk)
            or _ends_mid_number(base + chunk)
            or chunk[-1] in "的和与及"
        ):
            chunk = chunk[:-1]
        if len(chunk) >= 2:
            return base + chunk
    head = re.split(r"[和与及]", obj, maxsplit=1)[0]
    if head and head != obj and len(base) + len(head) <= _TITLE_HARD and not _ends_abruptly(head):
        return base + head
    return base


def _repair_headline(text: str) -> str:
    body = _TIME_PREFIX.sub("", _drop_leading_fragment(_compact(text)))
    if not body:
        return ""
    options: list[tuple[int, int, str, str]] = []
    for index, verb in (_tuple_action(body), _narrative_action(body)):
        if index < 0 or not verb:
            continue
        subject = _normalize_subject(body[:index])
        if _subject_ok(subject):
            options.append((index, -len(verb), subject, verb))
    if not options:
        return ""
    options.sort()
    index, _neg, subject, verb = options[0]
    rest = body[index + len(verb):]
    obj = rest
    if verb in _PRICE_NARRATIVE:
        later, later_verb = _tuple_action(rest)
        if later > 0 and later_verb in _GLUE_FOLLOWUPS:
            obj = rest[:later]
    base = f"{subject}{verb}"
    if len(base) > _TITLE_HARD or _ends_abruptly(base) or _ends_split_verb(base):
        return ""
    return _fit_object(base, obj)


_SIGNING_PARTY = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9]{2,10})[与和及]"
    r"([\u4e00-\u9fffA-Za-z0-9]{2,10}?)"
    r"(?=战略合作|签署|签订|签约|达成|合作)"
)


def _signing_parties(text: str) -> tuple[str, str]:
    """签约、战略合作标题里用与/和连起来的双方。"""
    body = _compact(text)
    body = re.sub(r"([\u4e00-\u9fffA-Za-z0-9]{2,10})[：:]与", r"\1与", body)
    if not any(cue in body for cue in ("签约", "签署", "签订", "战略合作")):
        return "", ""
    match = _SIGNING_PARTY.search(body)
    if not match:
        return "", ""
    left, right = match.group(1), match.group(2)
    if left in _NOT_SUBJECT or right in _NOT_SUBJECT or left in _SOURCE_NAMES or right in _SOURCE_NAMES:
        return "", ""
    return left, right


def _signing_title(text: str) -> str:
    """双方都留下。长标题被从「与」切开时，收成一句仍带对手方的签约标题。"""
    left, right = _signing_parties(text)
    if not left or not right:
        return ""
    body = _compact(text)
    if "签约" in body and "战略合作" in body:
        title = f"{left}与{right}战略合作签约"
    elif "签约" in body:
        title = f"{left}与{right}签约"
    elif "签署" in body:
        title = f"{left}与{right}签署战略合作" if "战略合作" in body else f"{left}与{right}签署合作"
    elif "签订" in body:
        title = f"{left}与{right}签订战略合作" if "战略合作" in body else f"{left}与{right}签订合作"
    elif "战略合作" in body:
        title = f"{left}与{right}战略合作签约"
    else:
        return ""
    if _display_clause_ok(title) or _title_ok(title):
        return title
    return ""


def _keeps_signing_parties(title: str, source: str) -> bool:
    left, right = _signing_parties(source)
    if not left:
        return True
    return left in title and right in title


def _prefer_signed(chosen: str, body: str, signed: str) -> str:
    """完整分句里双方都在就留原句。被截短或丢掉一方时，改用带双方的签约标题。"""
    if not signed:
        return chosen
    if not _keeps_signing_parties(chosen, body):
        return signed
    compact_body = _compact(body)
    compact_chosen = _compact(chosen)
    if compact_chosen and compact_chosen != compact_body and len(compact_chosen) > len(signed):
        return signed
    return chosen


def _dropped_signing_party(title: str, source: str) -> bool:
    text = _compact(title)
    if not text or not any(cue in text for cue in ("签约", "签署", "签订", "战略合作")):
        return False
    for raw in source.split("\n"):
        left, right = _signing_parties(raw)
        if left and right and (left not in text or right not in text):
            return True
    return False


def _fit_clause(clause: str) -> str:
    compact = _drop_leading_fragment(_strip_source_prefix(clause))
    if not compact or _watch_or_list_fragment(compact):
        return ""
    if _display_clause_ok(compact) or _title_ok(compact):
        return compact
    repaired = _repair_headline(compact)
    if repaired and (_display_clause_ok(repaired) or _title_ok(repaired)):
        return repaired
    return ""


def _representative_clause(text: str) -> str:
    body = _prepare_headline(text)
    if not body:
        return ""
    chosen = ""
    for part in _CLAUSE.split(body):
        fitted = _fit_clause(part)
        if fitted:
            chosen = fitted
            break
    return _prefer_signed(chosen, body, _signing_title(body))


def _title_cache_key(fingerprint: str) -> str:
    return f"title-v{_TITLE_CACHE_VERSION}:{fingerprint}"


def _purge_stale_title_cache() -> None:
    prefix = f"title-v{_TITLE_CACHE_VERSION}:"
    now = time.monotonic()
    for key in [key for key in _LLM_CACHE if not str(key).startswith(prefix)]:
        _LLM_CACHE.pop(key, None)
    # 过期的 _miss 也要清掉，否则长期运行会越堆越多（同一次长会话里反复重算快照）。
    for key, cached in [
        (key, cached) for key, cached in _LLM_CACHE.items()
        if isinstance(cached, dict) and cached.get("_miss")
    ]:
        at = cached.get("_miss_at")
        if not isinstance(at, (int, float)) or (now - at) >= _MISS_TTL_S:
            _LLM_CACHE.pop(key, None)


def _hint(now: datetime, trading: date, chosen: date, fallback: bool, updated_hm: str) -> str:
    clock = f" · 更新于 {updated_hm}" if updated_hm else ""
    if fallback:
        return f"暂无{_month_day(trading)}数据，显示{_month_day(chosen)}{clock}"
    if _auction_still_ahead(now, chosen):
        pending = f"交易日 {_month_day(chosen)} · 等待竞价" if chosen != now.date() else "等待竞价"
        return f"{pending}{clock}"
    if chosen != now.date():
        return f"交易日 {_month_day(chosen)}{clock}"
    return f"更新于 {updated_hm}" if updated_hm else ""


def _month_day(day: date) -> str:
    return f"{day.month}月{day.day}日"


def _is_section_label(name: object) -> bool:
    """栏目、材料分区，以及没有动作的点评、重点，都不是事件。"""
    text = _TRAILING_ELLIPSIS.sub("", _compact(str(name or "")))
    if not text:
        return False
    if text == "重点" or _SECTION_MATERIAL.fullmatch(text):
        return True
    if _tuple_action(text)[0] >= 0 or _narrative_action(text)[0] >= 0:
        return False
    return "点评" in text or text.startswith("重点")


_COMPANY_SUFFIXES = (
    "股份有限公司", "有限责任公司", "有限公司", "集团股份", "控股集团",
    "科技", "股份", "集团", "控股", "银行", "证券", "医药", "电子", "能源",
    "实业", "材料", "生物", "光电", "通信", "传媒", "地产", "汽车", "化工",
    "矿业", "资源", "国际", "电力", "新材", "信息", "网络", "软件", "智能",
    "环保", "装备", "机械", "半导体",
)
# 这些字出现在标题里，说明不是光秃的一个名字。
_NAME_GLUE = frozenset("的了在将把被从对而或已正再又也还就都与和")


def _is_source_only(text: str) -> bool:
    """整段标题只是媒体名，例如央视财经、财联社电、金十快讯。"""
    body = _compact(text)
    for tail in ("快讯", "报道", "消息", "电讯", "电", "讯"):
        if body.endswith(tail) and len(body) - len(tail) >= 2:
            body = body[: -len(tail)]
            break
    return body in _SOURCE_NAMES


def _is_bare_entity(text: str) -> bool:
    """整段标题只是公司或证券简称，里面没有动作。"""
    body = _compact(text)
    if not body or _tuple_action(body)[0] >= 0 or _narrative_action(body)[0] >= 0:
        return False
    if any(char.isdigit() for char in body) or any(char in _NAME_GLUE for char in body):
        return False
    if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{2,16}", body):
        return False
    if any(body.endswith(suffix) and len(body) > len(suffix) for suffix in _COMPANY_SUFFIXES):
        return True
    return len(body) <= 8


def _title_name_reject_reason(text: str) -> str:
    if _is_source_only(text):
        return "只有来源名"
    if _is_bare_entity(text):
        return "只有主体没有动作"
    return ""


def _is_commentary(text: str) -> bool:
    """侧面佐证、看好、关注、建议这类说法，没有主体和动作时不是事件。"""
    body = _compact(text)
    if not body or not any(cue in body for cue in _COMMENTARY_CUES):
        return False
    # 「要求」「实施」「印发」这类政策动词不在 _split_headline 的动词表里，
    # 切分拿不到动作。于是「中共中央、国务院要求全面实施"人工智能+"行动…
    # 分析师看好…」整条被当成点评丢掉 —— 那是国务院级政策，只因句尾挂了
    # 一句点评就被连坐。豁免要同时满足两点：句里有国务院这类具名主体
    # （「数据印证需求正在回暖」这种叙事句没有），且主体和动作站得住
    # （_has_gate_subject_action 含词性和政策兜底）。实测它能把政策类
    # （放行）和纯点评/研报口吻（仍拦）分开。
    if _explicit_entities(body) and _has_gate_subject_action(body):
        return False
    lead, action, _obj = _split_headline(body)
    return not (action and _subject_ok(lead))


def _proper_opener(prefix: str) -> str:
    """冒号前的公司名也算专名。媒体名和「公司」本身不是。"""
    parts = [part for part in re.split(r"[：:]", prefix.strip("：:")) if part]
    while parts and (
        parts[-1] in _GENERIC_SUBJECT_HEADS
        or parts[-1] in _SOURCE_NAMES
        or parts[-1] in _NOT_SUBJECT
    ):
        parts.pop()
    if not parts:
        return ""
    opener = parts[-1]
    if len(opener) < 2 or len(opener) > 12:
        return ""
    if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9]+", opener):
        return ""
    if opener in _NOT_SUBJECT or opener in _STOP or opener in _GENERIC_TOKENS or opener in _SOURCE_NAMES:
        return ""
    return opener


def _named_subject_swapped(title: str, source: str) -> bool:
    """标题用「公司」换掉了原标题里的专名。对照原始标题，不先按分句清掉冒号前的公司名。"""
    text = _compact(title)
    generic = next((word for word in _GENERIC_SUBJECT_HEADS if text.startswith(word)), "")
    if not generic or not source:
        return False
    rest = text[len(generic):]
    if len(rest) < 2:
        return False
    for raw in source.split("\n"):
        src = _compact(raw)
        if len(src) < 4:
            continue
        for needle in (text, rest):
            index = src.find(needle)
            if index < 2:
                continue
            opener = _proper_opener(src[:index])
            if opener and opener not in text:
                return True
    return False


def _llm_title_reject_reason(title: str, source: str = "") -> str:
    """模型标题的拒绝原因。空字符串表示可以上屏。

    字数只卡到 48。标题中间的年份、价格（2026、13.1元）不是截断；
    只有整句停在数字上才算数字被截断。
    """
    text = _compact(title)
    if not text or text == "事件":
        return "空标题"
    if _is_commentary(text) or _watch_or_list_fragment(text):
        return "不是具体事件"
    if _misordered_title(text):
        return "语序颠倒"
    blocked = _surface_block_reason(text)
    if blocked:
        return blocked
    name_reason = _title_name_reject_reason(text)
    if name_reason:
        return name_reason
    debris = _file_debris_reason(text)
    if debris:
        return debris
    if _title_incoherent(text):
        return "标题不连贯"
    if len(text) < 6:
        return "过短"
    if len(text) > _LLM_TITLE_MAX:
        return "超过48字"
    if _is_section_label(text):
        return "栏目标签"
    if text[0].isdigit():
        return "以数字开头"
    if any(char in text for char in "【】[]"):
        return "含括号"
    if "..." in text or "…" in text:
        return "省略号"
    if _starts_fragment(text):
        return "半句话"
    if _ends_abruptly(text):
        return "结尾不完整"
    if _ends_split_verb(text):
        return "动词被截断"
    if _ends_mid_number(text):
        return "数字被截断"
    if sum(1 for char in text if "\u4e00" <= char <= "\u9fff") < 2:
        return "中文不足"
    if _cut_glued_sentence(text) != text:
        return "粘连了下一句"
    if len(text) > _TITLE_LIMIT and _core_event_clause(text) != text:
        return "标题过长"
    if source and _named_subject_swapped(text, source):
        return "主体被换成泛称"
    if source and _dropped_signing_party(text, source):
        return "缺少合作方"
    if source and _cuts_any(text, [line for line in source.split("\n") if line.strip()]):
        return "截断过短"
    gate = _display_gate_reason(text, [line for line in source.split("\n") if line.strip()])
    # 门槛里的「缺少主体或动作」继续走原来的宾语检查，原因文案保持不变。
    if gate and gate != "缺少主体或动作":
        return gate
    lead, action, obj = _split_headline(text)
    if not (action and obj and _subject_ok(lead) and _concrete_subject(lead)):
        return "缺少主体、动作或宾语"
    return gate


def _llm_title_ok(title: str) -> bool:
    """模型标题：完整中文，有主体、动作和宾语，且不是钟点、标签或半句。"""
    return _llm_title_reject_reason(title) == ""


def _has_emoji(text: str) -> bool:
    for char in text:
        code = ord(char)
        if code >= 0x1F000 or 0x2600 <= code <= 0x27BF:
            return True
        if code > 0x2100 and unicodedata.category(char) == "So":
            return True
    return False


def _fallback_title_ok(name: object) -> bool:
    """回退标题和模型标题、展示截断用同一道门槛。"""
    return _display_gate_reason(str(name or "")) == ""


def _cleaned_display_ok(name: object) -> bool:
    """关键词回退不要求满 6 字，但钟点、括号、省略号和栏目标题不能上屏。"""
    text = _compact(str(name or ""))
    if (
        _title_incoherent(text)
        or _has_file_debris(text)
        or _is_commentary(text)
        or _watch_or_list_fragment(text)
        or _misordered_title(text)
        or _surface_block_reason(text)
    ):
        return False
    if _core_event_clause(text) != text:
        return False
    lead, action, _obj = _split_headline(text)
    if action and not _concrete_subject(lead):
        return False
    if not _display_name_ok(text) or _is_section_label(text):
        return False
    if text[0].isdigit() or any(char in text for char in "【】[]（）()"):
        return False
    if "..." in text or "…" in text:
        return False
    return _display_gate_reason(text) == ""


def _event_title_sources(event: dict) -> list[str]:
    sources = [str(item) for item in (event.get("headlines") or [])]
    sources.append(str(event.get("headline") or ""))
    sources.append(str(event.get("name") or ""))
    return [text for text in sources if text.strip()]


def _drops_known_action(short: str, full: str) -> bool:
    """截完以后原句里的动作不在了，这句不能上屏。"""
    _lead, verb, _obj = _split_headline(full)
    return bool(verb) and verb not in _compact(short)


def _is_harmful_cutoff(short: str, long: str) -> bool:
    """半截标题：切开了英文或数字，丢掉了动作，或把没超长的句子收成只有动作。"""
    short_text = _compact(short)
    long_text = _compact(long)
    if not short_text or len(short_text) < 4 or len(short_text) >= len(long_text):
        return False
    if not long_text.startswith(short_text):
        return False
    if _cut_glued_sentence(long_text) == short_text:
        return False
    rest = long_text[len(short_text):]
    if re.fullmatch(r"[.。…]+", rest):
        return False
    nxt = rest[0]
    prev = short_text[-1]
    if _is_ascii_alnum(prev) and _is_ascii_alnum(nxt):
        return True
    if _drops_known_action(short_text, long_text):
        return True
    return (
        nxt not in "，。！？、；："
        and len(long_text) <= _TITLE_DISPLAY_MAX
        and not _ends_abruptly(long_text)
    )


def _cuts_any(name: str, sources: list[str]) -> bool:
    for raw in sources:
        if _is_harmful_cutoff(name, raw) or _is_harmful_cutoff(name, _cleaned_headline(raw)):
            return True
    return False


def _starts_mark(text: str, marks: tuple[str, ...]) -> bool:
    return any(text.startswith(mark) for mark in marks)


def _headline_tier(source: str, raw: str) -> int:
    """公司公告/交易所，官方，财联社、华尔街见闻和格隆汇，外媒，群聊。数字越小越优先。

    新华社、央视、证监会经常夹在财联社稿的开头，所以看剥标签之前的原标题。
    """
    key = str(source or "").strip().lower()
    text = _compact(raw)
    bracket = _SOURCE_BRACKET.match(text)
    bracket_text = bracket.group(0) if bracket else ""
    lead = text[bracket.end():] if bracket else text
    lead = _CLOCK_PREFIX.sub("", lead).lstrip("：:|｜")
    filing = key in _FILING_SOURCES or lead.startswith("公告") or _starts_mark(lead, _EXCHANGE_MARKS)
    if filing or _starts_mark(bracket_text.lstrip("【[（("), _EXCHANGE_MARKS):
        return 0
    if _starts_mark(lead, _OFFICIAL_MARKS) or _starts_mark(bracket_text.lstrip("【[（("), _OFFICIAL_MARKS):
        return 1
    if key in _WIRE_SOURCES:
        return 2
    if key in _FOREIGN_SOURCES:
        return 3
    if key in _CHAT_SOURCES:
        return 4
    return 5


def _display_stitch(body: str) -> str:
    """公司或机构冒号接回主体。交易所抬头保持原样，不当成公司名粘上去。

    只去掉这一处冒号。后面的顿号、逗号按原文留下。
    """
    marks = list(_CLAUSE.finditer(body))
    if not marks or marks[0].group(0) not in "：:":
        return body
    head = body[:marks[0].start()].strip()
    if len(marks) == 1:
        nxt = body[marks[0].end():].strip()
        nxt_sep = ""
        tail = ""
    else:
        nxt = body[marks[0].end():marks[1].start()].strip()
        nxt_sep = marks[1].group(0)
        tail = body[marks[1].end():]
    if not head or not nxt or _starts_mark(head, _EXCHANGE_MARKS):
        return body
    joined = _join_colon_head(head, nxt)
    if not joined and _colon_subject(head) and (
        _tuple_action(nxt)[0] == 0 or _narrative_action(nxt)[0] == 0
    ):
        joined = head + nxt
    if not joined:
        return body
    if not nxt_sep:
        return joined
    return joined + nxt_sep + tail


_LEAD_ATTRIBUTION = re.compile(r"^(?:据报道|消息称|报道)")
_GLUED_LIST_INDEX = re.compile(r"(?<=[\u4e00-\u9fff])[0-9](?=[\u4e00-\u9fff])")


def _strip_lead_attribution(text: str) -> str:
    """句首的「报道 / 据报道 / 消息称」不是主体。"""
    body = text
    changed = True
    while body and changed:
        changed = False
        match = _LEAD_ATTRIBUTION.match(body)
        if match:
            body = body[match.end():].lstrip("：:，,|｜ ")
            changed = True
    return body


def _quote_delta(stack: list[str], char: str) -> None:
    """直引号成对开合。弯引号和直角引号按成对括号。撇号不当引号。"""
    if char == '"':
        if stack and stack[-1] == '"':
            stack.pop()
        else:
            stack.append('"')
        return
    if char in "“「『":
        stack.append(char)
        return
    opener = {"”": "“", "」": "「", "』": "『"}.get(char)
    if opener and stack and stack[-1] == opener:
        stack.pop()


def _quote_open(text: str) -> bool:
    stack: list[str] = []
    for char in text:
        if char == '"' or char in "“「『”」』":
            _quote_delta(stack, char)
    return bool(stack)


def _sentence_end_index(text: str, start: int = 0) -> int:
    """引号外面的下一个。；！？。没有就返回 -1。"""
    stack: list[str] = []
    for index, char in enumerate(text):
        if char == '"' or char in "“「『”」』":
            _quote_delta(stack, char)
        if index < start:
            continue
        if char in "。；！？" and not stack:
            return index
    return -1


def _weak_first_clause(text: str) -> bool:
    """「统一涨价10%」这种太短，或还没有主体和动作。"""
    if not text or len(text) < 8:
        return True
    return not _has_gate_subject_action(text)


def _close_open_quotes(shown: str, original: str) -> str:
    """截完以后引号还开着，就沿原文补到合上。对不上原文时保持原样。"""
    if not shown or not _quote_open(shown):
        return shown
    source = re.sub(r"\s+", "", original or "")
    index = source.find(shown)
    if index < 0:
        return shown
    end = index + len(shown)
    while end < len(source) and _quote_open(source[index:end]):
        end += 1
    extended = source[index:end].strip("，。！？、；：")
    return extended or shown


def _clip_original_length(text: str) -> str:
    """第一句完整就停在。；！？。太短或没有主体时带上下一句。引号里不截。"""
    body = _compact(text, keep_quotes=True)
    cut = _sentence_end_index(body)
    if cut < 0:
        shown = body
    else:
        first = body[:cut]
        if _weak_first_clause(first):
            nxt = _sentence_end_index(body, cut + 1)
            shown = body if nxt < 0 else body[:nxt]
        else:
            shown = first
    return _close_open_quotes(shown.strip("，。！？、；："), body)


def _display_original(text: str) -> str:
    """只去来源前缀、括号标签、券商组名和句首转述。不改写成短标题。"""
    body = _strip_lead_attribution(_strip_source_prefix(text, keep_quotes=True))
    if not body or body == "事件":
        return ""
    body = _display_stitch(body)
    body = _clip_original_length(body)
    if not body or body == "事件":
        return ""
    return body


def _report_chatter(text: str) -> bool:
    """研报口吻和编号清单不能当主标题。"""
    body = _compact(text)
    if not body:
        return False
    if "1e+" in body.lower() or any(mark in body for mark in ("更新", "把握机会")):
        return True
    # 工信部建议加快部署是政策动作。没有主体的「建议关注」仍是研报口吻。
    if "建议" in body and _is_commentary(body):
        return True
    return bool(_LIST_MARKER.search(body) or _GLUED_LIST_INDEX.search(body))


def _research_headline_ok(title: str) -> bool:
    """研究来源里写清楚的公司或主题标题。研报口吻和多主题清单仍然不用。"""
    cleaned = _display_original(title)
    if not cleaned or _report_chatter(cleaned):
        return False
    if _TOPIC_JOIN.search(cleaned) or len(_BRIEF_MARK.findall(cleaned)) >= 2:
        return False
    return _original_reject_reason(cleaned) == ""


def _headline_blocked(source: str, title: str) -> bool:
    """星球、ima、钉钉里的研报口吻不拿来当主标题。写清楚的公司或主题标题可以。

    研报文件名里的「更新」会先被剥掉。剥完还剩「十五五政策定调」这种句子时可以上屏。
    """
    if str(source or "").strip().lower() in _RESEARCH_SOURCES:
        return not _research_headline_ok(title)
    cleaned = _display_original(title)
    return bool(cleaned and _report_chatter(cleaned))


def _original_reject_reason(title: str, sources: list[str] | None = None) -> str:
    """原标题门槛。不卡 20 字。寒暄、纯点评、研报标签和空标题仍然丢掉。"""
    text = _compact(str(title or ""))
    blobs = [str(item) for item in (sources or []) if str(item).strip()]
    if not text or text == "事件":
        return "空标题"
    if any(mark in text for mark in _CHATTER_MARKS) or any(mark in text for mark in _NARRATIVE_MARKS):
        return "不是具体事件"
    if "..." in text or "…" in text:
        return "省略号"
    if _BROKER_NOTE.match(text) or _BROKER_TEAM_LEAD.match(text):
        return "券商前缀"
    # 「10月以来逾80家A股上市公司获机构调研」是正常标题。这条门槛本来拦的是
    # 「1. 」「2）」这类编号清单，日期开头（10月/12日）不该连坐。
    if text[0].isdigit() and not _DATE_LIKE_OPENING.match(text):
        return "以数字开头"
    if _is_commentary(text) or _watch_or_list_fragment(text) or _misordered_title(text):
        return "不是具体事件"
    blocked = _surface_block_reason(text)
    if blocked:
        return blocked
    if _title_incoherent(text) or _has_file_debris(text) or _is_section_label(text):
        return "不是具体事件"
    if _has_emoji(text) or _CJK_SLASH.search(text):
        return "不是具体事件"
    name_reason = _title_name_reject_reason(text)
    if name_reason:
        return name_reason
    boundary = _boundary_reason(text, blobs)
    if boundary:
        return boundary
    if blobs and _dropped_signing_party(text, "\n".join(blobs)):
        return "缺少合作方"
    if _bare_transitive(text):
        return "缺少宾语"
    if not _has_gate_subject_action(text) and not _informative_policy(text):
        return "缺少主体或动作"
    return ""


def _jieba():
    """词典只初始化一次。jieba 源码里的正则会报 SyntaxWarning，这里吞掉。"""
    global _JIEBA
    if _JIEBA is None:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            import jieba
            import jieba.posseg as posseg

            jieba.setLogLevel(logging.ERROR)
            jieba.initialize()
        _JIEBA = (jieba, posseg)
    return _JIEBA


def _segment(text: str) -> list[str]:
    """用词典切词。只看最后一个词是否完整，避免把「从容应」当成写完了。"""
    jieba = _jieba()[0]
    return [word for word in jieba.cut(_compact(text), HMM=False) if str(word).strip()]


def _pos_cut(text: str):
    posseg = _jieba()[1]
    return [word for word in posseg.cut(_compact(text), HMM=False) if str(word.word).strip()]


def _latin_cutoff_fragment(text: str) -> bool:
    """Vicor对V 这种：前面有完整英文词，结尾只剩一个字母。"""
    body = _compact(text)
    if len(body) < 4:
        return False
    words = _LATIN_WORD.findall(body)
    if len(words) < 2 or len(words[-1]) != 1:
        return False
    return any(len(word) >= 3 for word in words[:-1])


def _cutoff_pair(left: dict, right: dict) -> bool:
    """半截标题和完整标题是同一件事，不受 6 小时限制。"""
    left_title = str(left.get("title") or "")
    right_title = str(right.get("title") or "")
    return _is_harmful_cutoff(left_title, right_title) or _is_harmful_cutoff(right_title, left_title)


def _trailing_latin_fragment(text: str, blobs: list[str]) -> bool:
    body = _compact(text)
    match = _TRAILING_LATIN.search(body)
    if not match:
        return False
    token = match.group(0)
    if token.isdigit():
        return False
    if len(token) < 2:
        return True
    for blob in blobs:
        for word in _LATIN_WORD.findall(_compact(blob)):
            if len(word) > len(token) and word.startswith(token):
                return True
    return False


def _measure_tail_after_number(words: list[str], last: str) -> bool:
    """元/吨、成/股：量词跟在数字后面（中间可能再隔一个量词），不是词被切断。

    「报114600元/吨」切出来是「114600元」「吨」两个词，只看前一个词的话
    末字是「元」不是数字，老代码在这里误判「词中间截断」。往前多找一个词，
    见到以数字或中文数字结尾的就放行。
    """
    if last not in _MEASURE_TAILS:
        return False
    for word in reversed(words[:-1]):
        if word and (word[-1].isdigit() or word[-1] in _CN_NUMERALS):
            return True
    return False


def _cut_off_by_longer_peer(body: str, blobs: list[str]) -> bool:
    """同簇里有没有另一条更长、且以本条整体开头的标题。有才敢判词中间截断。

    jieba 对未登录词本就会切成单字：「俄罗斯制裁法」「易方达创新高」
    「三大指数集体低开」都是完整句子，老代码一律判「词中间截断」，
    实测 4/4 抽样全错。改成与 _is_harmful_cutoff 同一口径 —— 只有簇里
    另有一条更长、以本条整体开头（说明本条是被切剩的半句）时才判。
    """
    for blob in blobs or []:
        other = _compact(str(blob or ""))
        if len(other) > len(body) and other.startswith(body):
            return True
    return False


def _boundary_reason(text: str, blobs: list[str]) -> str:
    body = _compact(text)
    if not body:
        return "空标题"
    if (
        _ends_split_verb(body)
        or _ends_mid_number(body)
        or any(body.endswith(tail) for tail in _FALLBACK_BAD_TAILS)
    ):
        return "词中间截断"
    if _trailing_latin_fragment(body, blobs):
        return "外文被截断"
    words = _segment(body)
    if not words:
        return ""
    last = words[-1]
    # 「从容应对」是完整词，末字「对」不是截断。「从容应」的最后一个字才是。
    if len(last) < 2 and _ends_abruptly(body):
        return "词中间截断"
    if len(last) == 1 and "\u4e00" <= last <= "\u9fff":
        prev = words[-2] if len(words) >= 2 else ""
        pair = prev + last
        if pair in _PRICE_NARRATIVE or pair in _ACTIONS or pair in _GATE_EXTRA_VERBS:
            return ""
        # 13.1元、六成、114600元/吨：量词跟在数字后面，不是把「从容应对」切成「应」。
        if _measure_tail_after_number(words, last):
            return ""
        # 单字收尾不等于截断，见 _cut_off_by_longer_peer。
        if _cut_off_by_longer_peer(body, blobs):
            return "词中间截断"
        return ""
    return ""


def _hedged_subject(lead: str) -> bool:
    """可能、还会、几次不是主体。句里已经点了美联储这类机构时仍算有主体。"""
    text = _compact(lead)
    if not text or _explicit_entities(text):
        return False
    return any(cue in text for cue in _GATE_HEDGES)


def _pos_entity(lead: str) -> bool:
    """词性或价格、公司、电网这类标记。用来认出动词表里没有的熄灯、遭袭。"""
    if _explicit_entities(lead):
        return True
    if any(mark in lead for mark in _POS_ENTITY_MARKS):
        return True
    for word in _pos_cut(lead):
        if word.flag in _POS_ENTITY_FLAGS and len(word.word) >= 2:
            return True
        if word.flag == "eng" and len(word.word) >= 3 and not word.word.isdigit():
            return True
    return False


def _pos_subject_action(text: str) -> bool:
    """动词表没覆盖时，用词性看有没有主体和至少两个字的动词。"""
    words = _pos_cut(text)
    for index, word in enumerate(words):
        if not str(word.flag).startswith("v") or len(word.word) < 2:
            continue
        lead = "".join(item.word for item in words[:index])
        if not (_subject_ok(lead) and _concrete_subject(lead) and not _hedged_subject(lead)):
            continue
        if _pos_entity(lead):
            return True
    return False


def _verb_on_token_boundary(text: str, start: int, verb: str) -> bool:
    """动词不能从某个词的中间跨进下一个词。「新增长极」里的「新增」不算，「价格上涨」里的「上涨」算。"""
    if start < 0 or not verb:
        return False
    end = start + len(verb)
    pos = 0
    for word in _segment(text):
        word_end = pos + len(word)
        if word_end <= start:
            pos = word_end
            continue
        if pos <= start and word_end >= end:
            return True
        if pos < start or word_end > end:
            return False
        pos = word_end
        if pos == end:
            return True
    return False


def _has_gate_subject_action(text: str) -> bool:
    body = _compact(text)
    lead, verb, _obj = _split_headline(body)
    if verb and _verb_on_token_boundary(body, len(lead), verb):
        return _subject_ok(lead) and _concrete_subject(lead) and not _hedged_subject(lead)
    for extra in _GATE_EXTRA_VERBS:
        index = body.find(extra)
        if index < 2:
            continue
        head = body[:index]
        if _subject_ok(head) and _concrete_subject(head):
            return not _hedged_subject(head)
    # 动词表里有这个词，但主体不合格。不要再改用词性把「结构性降息」放行。
    if (
        _tuple_action(body)[0] >= 0
        or _narrative_action(body)[0] >= 0
        or _directive_action(body)[0] >= 0
    ):
        return False
    return _pos_subject_action(body) or _informative_policy(body)


def _bare_transitive(text: str) -> bool:
    """「财政部发布」只有主体和及物动词，宾语被丢掉了。签约双方写全时宾语可以在动词前。"""
    body = _compact(text)
    lead, verb, obj = _split_headline(body)
    if verb not in _OBJECT_VERBS or _compact(obj):
        return False
    if verb in ("签约", "签署", "签订") and any(
        mark in body for mark in ("与", "和", "协议", "战略合作", "备忘录")
    ):
        return False
    return _subject_ok(lead) and _concrete_subject(lead)


def _informative_policy(text: str) -> bool:
    """完整的政策原标题。比「财政部发布」更该上屏。"""
    body = _compact(text)
    if len(body) < 12 or _bare_transitive(body):
        return False
    if not body.endswith(("政策", "方案", "规划", "措施")):
        return False
    return any(str(word.flag).startswith("v") and len(word.word) >= 2 for word in _pos_cut(body))


def _gated_text(text: str) -> str:
    """展示、回退和截断都只留下这道门槛通过的句子。"""
    body = _compact(str(text or ""))
    if body and _display_gate_reason(body) == "":
        return body
    return ""


def _display_gate_reason(title: str, sources: list[str] | None = None) -> str:
    """展示门槛。模型标题和回退标题都走这里。空字符串表示可以上屏。"""
    text = _compact(str(title or ""))
    blobs = [str(item) for item in (sources or []) if str(item).strip()]
    if not text or text == "事件":
        return "空标题"
    if any(mark in text for mark in _CHATTER_MARKS) or any(mark in text for mark in _NARRATIVE_MARKS):
        return "不是具体事件"
    if "..." in text or "…" in text:
        return "省略号"
    if any(char in text for char in "【】[]（）()"):
        return "含括号"
    if _BROKER_NOTE.match(text) or _BROKER_TEAM_LEAD.match(text):
        return "券商前缀"
    # 日期开头（10月/12日）不是编号清单，见 _DATE_LIKE_OPENING。
    if text[0].isdigit() and not _DATE_LIKE_OPENING.match(text):
        return "以数字开头"
    if _cut_glued_sentence(text) != text:
        return "粘连了下一句"
    if len(text) > _TITLE_LIMIT and _core_event_clause(text) != text:
        return "标题过长"
    if _is_commentary(text) or _watch_or_list_fragment(text) or _misordered_title(text):
        return "不是具体事件"
    blocked = _surface_block_reason(text)
    if blocked:
        return blocked
    if _title_incoherent(text) or _has_file_debris(text) or _is_section_label(text):
        return "不是具体事件"
    if _has_emoji(text) or _CJK_SLASH.search(text):
        return "不是具体事件"
    boundary = _boundary_reason(text, blobs)
    if boundary:
        return boundary
    if blobs and _dropped_signing_party(text, "\n".join(blobs)):
        return "缺少合作方"
    if _bare_transitive(text):
        return "缺少宾语"
    if not _has_gate_subject_action(text):
        return "缺少主体或动作"
    return ""


def _candidate_rows(event: dict) -> list[tuple[str, str]]:
    rows = event.get("_candidates")
    found: list[tuple[str, str]] = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            if title:
                found.append((str(row.get("source") or ""), title))
    if found:
        return found
    return [("", text) for text in _event_title_sources(event)]


def _first_original_title(rows: list[tuple[str, str]]) -> str:
    """按来源档次试新闻原标题。研报和群聊跳过。最可信的一条不合格，就试下一条。"""
    ordered = sorted(enumerate(rows), key=lambda item: (_headline_tier(item[1][0], item[1][1]), item[0]))
    blobs = [title for _source, title in rows]
    passing: list[str] = []
    for _index, (source, title) in ordered:
        if _headline_blocked(source, title):
            continue
        cleaned = _display_original(title)
        if not cleaned or cleaned in passing:
            continue
        if _original_reject_reason(cleaned, blobs):
            continue
        passing.append(cleaned)
    for candidate in passing:
        if any(candidate != other and _is_harmful_cutoff(candidate, other) for other in passing):
            continue
        return candidate
    return ""


def _first_credible_title(rows: list[tuple[str, str]]) -> str:
    return _first_original_title(rows)


def _fallback_cleaned_name(event: dict) -> str:
    return _first_credible_title(_candidate_rows(event))


def _awaiting_news_title(event: dict) -> bool:
    """没有能用的新闻原标题时先留下，让模型试一条严格标题。"""
    rows = _candidate_rows(event)
    if not rows or _first_original_title(rows):
        return False
    return any(_headline_blocked(source, title) for source, title in rows)


def _ensure_shown_name(event: dict) -> None:
    """主标题优先用新闻原标题。没有时，严格门槛通过的模型标题顶上，否则留空。"""
    rows = _candidate_rows(event)
    original = _first_original_title(rows)
    subtitle = str(event.get("subtitle") or "")
    if original:
        event["name"] = original
        event["subtitle"] = subtitle
    elif (
        _awaiting_news_title(event)
        and subtitle
        and _llm_title_reject_reason(subtitle, _event_source_text(event)) == ""
    ):
        event["name"] = subtitle
        event["subtitle"] = ""
    else:
        event["name"] = ""
        event["subtitle"] = subtitle
    _drop_repeated_subtitle(event)
    event.pop("_llm_title_applied", None)


def _drop_repeated_subtitle(event: dict) -> None:
    """要点和主标题重复、几乎只是换个说法、或跟主标题不是同一件事时，留空。

    判定分三层：

    1. 完全相同、或要点整个包含在主标题里 —— 最直接的一种。
    2. 字符重合率（bigram Jaccard）高到 ``_SUBTITLE_SAME_TOPIC``：模型多数时候
       只是把主标题换个说法（「被立案」→「被立案并实施留置」），不值得占一行。
    3. 要点里没有一点实体或数字能和主标题对上 —— 模型写串了编号，把别的资讯
       写到了这一条（实测「纳斯达克金龙中国指数」拿到「WTI原油」那句）。
    """
    name = _compact(str(event.get("name") or ""))
    subtitle = _compact(str(event.get("subtitle") or ""))
    if not name or not subtitle:
        return
    if subtitle == name or subtitle in name:
        event["subtitle"] = ""
        return
    if _headline_similarity(name, subtitle) >= _SUBTITLE_SAME_TOPIC:
        event["subtitle"] = ""
        return
    if not _shares_subject_or_number(name, subtitle):
        event["subtitle"] = ""


# 要点与主标题的 bigram 重合率到这一档就算「只是换个说法」。0.7 是评审给的
# 参考值；实测「被立案」→「被立案并实施留置」这类改写落在 0.6~0.75。
_SUBTITLE_SAME_TOPIC = 0.7
# 候选正文的标题要和主标题重合到这一档，才认这条正文属于本事件。
# 比 _SUBTITLE_SAME_TOPIC 松一档：候选标题是原始新闻标题，措辞和聚类后的
# 主标题常有差异（主标题取的是「最完整原标题」，可能是另一条候选的），
# 卡太死会把正确候选也挡掉。0.5 只拦「肉眼可见讲的是别的事」那种。
_BODY_TITLE_MIN = 0.5
# 候选归属的第二条通路：候选标题与**同簇其它候选标题**的字符覆盖率。
# 见 `_candidate_in_event`。0.6 的分界实测很宽：同事件的候选之间覆盖率
# 落在 0.97~1.00，不同事件的落在 0.00~0.26。
_BODY_PEER_COVER = 0.6


def _subtitle_entities(text: str) -> set[str]:
    """一句话里已登记的机构名和英文缩写。"""
    body = _compact(text)
    signals: set[str] = set()
    for entity in _explicit_entities(body):
        signals.add(entity)
    for latin in re.findall(r"[A-Za-z]{2,}", body):
        signals.add(latin.upper())
    return signals


# 要点的事实核对用。数字带上单位/百分号一起抓，避免「12」在正文里是「1200」的子串
# 也被当成命中（下面比对时按整段字符是否出现，不是按 token 边界）。
_NUMBER_TOKEN = re.compile(r"\d+(?:\.\d+)?%?")


def _subtitle_numbers(text: str) -> set[str]:
    """一句话里的数字（含小数点和百分号）。核对要点是否编造事实时用。"""
    return set(_NUMBER_TOKEN.findall(_compact(text)))


def _unbacked_facts(title: str, source: str) -> str:
    """要点里有没有正文/标题里找不到的数字或机构名。返回第一个对不上的，没有则空串。

    合并前约定的兜底：要点要写「主标题之外的事实」，那这个事实必须真的在素材里。
    数字是幻觉最集中的地方（少一个零、编一个比例），机构名次之。只查这两类 ——
    中文专名/地名抽取覆盖不全，一严就连带把正确补充误杀。

    数字整体字符比对：正文出现「1200万」时，要点写「1200」算命中（子串），
    写「120」不算（不是子串）。这样既宽松到不误杀，又能抓住凭空编造的数字。

    机构名只查**登记过的**（`_explicit_entities`），不查随手扫到的英文串 ——
    `iPhone18Pro` 会被切成 `PRO` 这种碎片，拿它去核对只会误杀正确的要点。
    比对统一转小写，`Pro`/`PRO`、`iPhone`/`IPHONE` 都要算命中。
    """
    if not source:
        return ""
    haystack = _compact(source).lower()
    for number in _subtitle_numbers(title):
        if number not in haystack:
            return f"数字{number}不在正文"
    for entity in _explicit_entities(_compact(title)):
        if entity.lower() in haystack:
            continue
        # 机构名可能只出现在标题侧（source 已含标题），仍找不到才算编造。
        return f"实体{entity}不在正文"
    return ""


def _shares_subject_or_number(name: str, subtitle: str) -> bool:
    """要点要和主标题讲同一件事，否则视为写串了编号。

    只看**实体**（机构名、英文缩写），不看数字 —— 要点带上一个主标题没有的数字
    恰恰是它的价值（「召回30064辆」→「涉及2022至2025款S级」），拿数字判错配会
    把正确补充全误杀。只在两边都有实体、且完全不相交时才判定为错配；任何一边
    实体不足就放行（中文专名抽取覆盖不到「柴油协议」这类，一严就连带误杀）。
    实测能抓住「纳斯达克金龙中国指数」拿到「WTI原油」这种整条串号的情况。
    """
    wanted = _subtitle_entities(name)
    got = _subtitle_entities(subtitle)
    if not wanted or not got:
        return True
    return bool(wanted & got)


def _event_is_cutoff_of(short: dict, host: dict) -> bool:
    short_texts = [str(short.get("name") or "")]
    short_texts.extend(title for _source, title in _candidate_rows(short))
    host_texts = [str(host.get("name") or "")]
    host_texts.extend(title for _source, title in _candidate_rows(host))
    for left in short_texts:
        for right in host_texts:
            if _is_harmful_cutoff(left, right):
                return True
    return False


def _absorb_cutoff_events(events: list[dict]) -> list[dict]:
    """半截簇并进完整事件。提及次数留在完整那条上，不随半截标题一起丢掉。"""
    drop: set[int] = set()
    for index, event in enumerate(events):
        host_index = None
        for other, host in enumerate(events):
            if other == index or other in drop:
                continue
            if _event_is_cutoff_of(event, host):
                host_index = other
                break
        if host_index is None:
            continue
        host = events[host_index]
        host["mentions"] = int(host.get("mentions") or 0) + int(event.get("mentions") or 0)
        host["red_count"] = int(host.get("red_count") or 0) + int(event.get("red_count") or 0)
        host["source_count"] = int(host.get("source_count") or 0) + int(event.get("source_count") or 0)
        item_ids = list(host.get("item_ids") or [])
        item_ids.extend(str(item) for item in (event.get("item_ids") or []))
        host["item_ids"] = item_ids
        headlines = list(host.get("headlines") or [])
        for title in event.get("headlines") or []:
            if title and title not in headlines:
                headlines.append(title)
        host["headlines"] = headlines
        candidates = list(host.get("_candidates") or [])
        candidates.extend(event.get("_candidates") or [])
        host["_candidates"] = candidates
        host["heat"] = round(float(host.get("heat") or 0) + float(event.get("heat") or 0), 4)
        drop.add(index)
    return [event for index, event in enumerate(events) if index not in drop]


def _has_stock_mapping(event: dict) -> bool:
    for item in event.get("mentioned_stocks") or []:
        if isinstance(item, dict) and str(item.get("key") or "").strip():
            return True
    return False


def _needs_model_title(event: dict) -> bool:
    return _first_original_title(_candidate_rows(event)) == ""


def _title_priority(event: dict) -> bool:
    """还没有能用的原标题，但已经映射到股票，或规则上是重要/重大。"""
    if not _needs_model_title(event):
        return False
    if _has_stock_mapping(event):
        return True
    return str(event.get("importance") or "") in {"重要", "重大"}


def _events_for_titles(events: list[dict]) -> list[dict]:
    """切片之外的股票映射和重要事件优先起标题。有原标题的位置让出来，调用次数不变。"""
    if len(events) <= _TITLE_SLICE:
        return list(events)
    head = list(events[:_TITLE_SLICE])
    pulled: list[dict] = []
    for event in events[_TITLE_SLICE:]:
        if len(pulled) >= _TITLE_PRIORITY_EXTRA:
            break
        if _title_priority(event):
            pulled.append(event)
    if not pulled:
        return head
    drop: set[int] = set()
    for index in range(len(head) - 1, -1, -1):
        if len(drop) >= len(pulled):
            break
        if not _needs_model_title(head[index]):
            drop.add(index)
    kept = [event for index, event in enumerate(head) if index not in drop]
    overflow = len(pulled) + len(kept) - _TITLE_SLICE
    if overflow > 0:
        kept = kept[:-overflow] if overflow < len(kept) else []
    return (pulled + kept)[:_TITLE_SLICE]


def _has_ashare_mapping(event: dict) -> bool:
    if any(str(name).strip() for name in (event.get("concepts") or [])):
        return True
    for item in event.get("mentioned_stocks") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "").strip() or str(item.get("key") or "").strip():
            return True
    return False


def _has_strong_market_cue(text: str) -> bool:
    if any(org in text for org in _ORG_SUBJECTS):
        return True
    return any(cue in text for cue in _STRONG_MARKET)


def _non_market_unmapped(event: dict) -> bool:
    """奖项、文体、体育，又没有 A 股概念或个股时，不进热榜。"""
    if _has_ashare_mapping(event):
        return False
    text = _event_text(event)
    if any(cue in text for cue in _HARD_NON_MARKET):
        return True
    return any(cue in text for cue in _SOFT_NON_MARKET) and not _has_strong_market_cue(text)


def _keep_shown_event(event: dict) -> bool:
    if _non_market_unmapped(event):
        return False
    name = str(event.get("name") or "")
    blobs = [title for _source, title in _candidate_rows(event)]
    return _original_reject_reason(name, blobs) == ""


def _subtitle_has_source_prefix(text: str) -> bool:
    if _is_source_only(text):
        return True
    if _SOURCE_BRACKET.match(text) or _SOURCE_LABEL.match(text) or _SOURCE_DATELINE.match(text):
        return True
    return bool(_BROKER_NOTE.match(text) or _BROKER_TEAM_LEAD.match(text))


# 替换符，以及 UTF-8 被当成 Latin-1 之后的典型字节。ASML、iPhone 对不上。
_REPLACEMENT_MARK = re.compile("[\ufffd\ufffe]|锟斤拷")
_MOJIBAKE_MARK = re.compile("(?:Ã[\u0080-\u00ff]|Â[\u0080-\u00ff]|â€)")


def _subtitle_garbled(text: str) -> bool:
    """只有替换符、错位编码，或汉字和字母数字加起来不到三成。"""
    if _REPLACEMENT_MARK.search(text) or _MOJIBAKE_MARK.search(text):
        return True
    if not text:
        return False
    readable = sum(1 for char in text if char.isalnum())
    return readable / len(text) < 0.30


def _subtitle_reject_reason(title: str, source: str = "") -> str:
    """一句话补充要点。丢掉空的、乱的、来源前缀、寒暄、过长，以及编造事实的。

    不套主标题那套主谓宾门槛，但**卡长度**（要一行放下）**和事实来源**（数字、
    机构名必须能在标题或正文里找到）。source 为空时跳过事实核对。
    """
    text = _compact(title)
    if not text or text == "事件":
        return "空标题"
    if any(mark in text for mark in _CHATTER_MARKS):
        return "寒暄"
    if _subtitle_has_source_prefix(text):
        return "来源前缀"
    if _subtitle_garbled(text):
        return "乱码"
    if len(text) > _SUBTITLE_MAX:
        return f"过长{len(text)}字"
    unbacked = _unbacked_facts(text, source)
    if unbacked:
        return unbacked
    return ""


def _cached_title_usable(cached: dict) -> bool:
    if cached.get("_miss"):
        # 过期就不再认这条缓存 —— 见 _MISS_TTL_S。老缓存没有 _miss_at 字段，
        # 按「已过期」处理，让它们重问一次拿到正确结果。
        at = cached.get("_miss_at")
        if not isinstance(at, (int, float)):
            return False
        return (time.monotonic() - at) < _MISS_TTL_S
    title = str(cached.get("title") or "")
    return not title or _subtitle_reject_reason(title) == ""


def _event_source_text(event: dict) -> str:
    """事件的全部可用素材：标题侧 + 正文。

    要点的事实核对（数字、机构名）必须对着**正文**查，不能只对着标题。早先只拼
    标题，导致任何引用了正文数字的正确要点都会被判「编造」。正文取不到时退回标题。
    """
    parts = [str(event.get("headline") or ""), str(event.get("name") or "")]
    parts.extend(str(item) for item in (event.get("headlines") or []))
    body = _event_body(event)
    if body:
        parts.append(body)
    return "\n".join(part for part in parts if part)


def _title_cover(left: str, right: str) -> float:
    """左侧标题的 bigram 有多大比例能在右侧找到（谁短看谁，取双向最大值）。

    与 ``_headline_similarity``（bigram Jaccard）的区别在这里是决定性的：
    Jaccard 的分母是**并集**，长度差一大就掉到 0.2 以下；覆盖率的分母是
    短的一侧，只问「短的那句话有没有被长的那篇盖住」。聚类后的事件主标题
    往往只有十几个字，而候选的原始标题是完整的一句话（「【冰轮环境】子公司
    顿汉布什近期收到4份海外数据中心冷源装备采购订单…」），拿 Jaccard 去量
    天然吃亏 —— 实测「顿汉布什获4份海外订单合计1.1亿美元」与其正文候选的
    Jaccard 只有 0.185、覆盖率却有 0.706，于是正文取不到、要点全空。
    """
    aa, bb = _norm_title(left), _norm_title(right)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    ga, gb = _title_grams(left), _title_grams(right)
    if not ga or not gb:
        return 0.0
    return max(len(ga & gb) / len(ga), len(ga & gb) / len(gb))


def _peer_titles(event: dict, *, exclude: str) -> list[str]:
    """同簇候选的原始标题，**排除与 ``exclude`` 同一个的那条**。

    排除很重要：簇的代表标题（headline）常常就是某条候选自己的标题，不排掉的话
    「拿自己比自己」覆盖率恒为 1.0，任何候选都会被采纳，等于没判。
    """
    bare = _compact(exclude)
    titles: list[str] = []
    for row in event.get("_candidates") or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if title and _compact(title) != bare:
            titles.append(title)
    for title in (event.get("headlines") or []):
        text = str(title).strip()
        if text and _compact(text) != bare:
            titles.append(text)
    headline = str(event.get("headline") or "").strip()
    if headline and _compact(headline) != bare:
        titles.append(headline)
    return titles


def _candidate_in_event(row: dict, event: dict) -> bool:
    """这条候选讲的是不是本事件这件事。

    只用 ``_headline_similarity(候选标题, 主标题) >= _BODY_TITLE_MIN`` 判会漏：
    主标题是**跨源重写**过的（模型/清洗产出），和任何一条候选的原始措辞都对不上。
    实测交易日 MISS 掉的 8 条，候选与主标题的 Jaccard 全部落在 0.13~0.24，
    离 0.5 差得远 —— 正文一条都取不到，要点只能留空（覆盖率卡在 13/20）。

    这里补一条更贴合事实的通路：候选标题与**同簇其它候选标题**比对。簇是先由
    ``_can_merge``（主体+动作+时间窗）聚出来的，簇内候选本来就都是同一件事，
    原始标题之间措辞天然接近 —— 实测同事件的覆盖率 0.97~1.00，不同事件的
    0.00~0.26，用 0.6 分开既捞得回漏掉的，也拦得住「簇里混进别条」。

    没有别的候选可比对时（单条稿的簇）直接采纳：整簇就是这一件事，不存在
    「混进别条」的风险，不认反而会把唯一的一篇正文丢掉。

    主体的显式实体命中仍然保留，用来兜住覆盖率因措辞改写而偏低的情况。
    """
    title = _compact(str(row.get("title") or ""))
    if not title:
        return False
    name = _compact(str(event.get("name") or ""))
    if name and (name in title or _headline_similarity(title, name) >= _BODY_TITLE_MIN):
        return True
    peers = _peer_titles(event, exclude=str(row.get("title") or ""))
    if not peers:
        return True
    return any(_title_cover(title, peer) >= _BODY_PEER_COVER for peer in peers)


def _body_from_text(name: str, text: str) -> str:
    """从一条资讯原文里取正文：剥掉开头的标题部分，返回剩下的新内容。

    **不能按「有没有换行」来判断哪部分是正文**。早先这里是
    ``body = "\\n".join(lines[1:]) if len(lines) > 1 else ""`` —— 假定第 1 行是
    标题、第 2 行起是正文。可是 cls(财联社)/wscn(华尔街见闻)/reuters/bloomberg/
    cnbc 的快讯大量是**单行**格式（``【标题】正文……`` 或 ``导语。正文……`` 全挤在
    一行，没有换行），这条判断会把一整段实质正文当成「无正文」直接丢掉，要点
    只能留空。实测交易日 421 条里「单行且 >60 字」（有正文却被丢）的占 42%：
    cls 68/91、wscn 37/76、reuters 21/26、bloomberg 18/19、cnbc 8/8 ——
    线上展示前 20 的要点覆盖率因此只有 10/20（评审门槛 14/20）。

    更要紧的是下游 ``_strip_repeated_lead`` **本来就处理了**「``【标题】正文``
    同一行」的剥离，但被这里的前置判断挡在门外，永远执行不到 —— 两处修复互相
    打架。所以这里改成：**单行文本不做任何前置淘汰，直接交给剥离逻辑**，
    剥离后还剩得下新内容就算正文，确实是纯标题/纯公告一句才判空。

    换行只是在「首行确实是标题」时的一个便利切分：首行与主标题对得上（或本身
    就短得像标题）才丢掉首行；对不上就把整段交出去，让 ``_strip_repeated_lead``
    按句子重合度去剥 —— 否则「导语在第一行、正文在第二行」的稿件会被连正文
    一起丢掉。
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    if len(lines) == 1:
        return lines[0]
    head = _compact(lines[0])
    # 首行像标题（与主标题重合，或本身就短）才丢；否则认为首行也是正文。
    looks_like_title = bool(head) and (
        (name and (name in head or _headline_similarity(name, head) >= _BODY_TITLE_MIN))
        or len(head) <= _FIRST_LINE_TITLE_MAX
    )
    if looks_like_title:
        return "\n".join(lines[1:]).strip()
    return text


def _event_body(event: dict) -> str:
    """事件里最完整的一条正文，去掉标题行、以及与主标题重合的开头后返回。

    要点要写「主标题没提到的事实」，素材必须来自正文。只给标题的话，模型手里
    没有任何新信息，只能把主标题换个说法（实测 20 条要点 20 条都是同义改写）。

    **只用和本事件对得上的那条候选**（见 ``_candidate_in_event``）。早先
    在匹配不到时退而取簇里最长的一条，簇里混进别的新闻时就把要点带偏 ——
    实测「中信证券：用景气和供给出清来应对高利率…」的要点写成了「沃勒预计还需
    加息两次」，那句来自同簇另一条「美联储理事沃勒表示…」的正文，中信这条正文
    里根本没有沃勒，`_unbacked_facts` 对着同一份错正文校验自然放行。

    判据早先是「候选标题与主标题的 bigram Jaccard ≥ 0.5」，但主标题是跨源重写
    出来的，与任何一条候选的原始措辞都对不上 —— 实测交易日被漏掉的 8 条事件，
    Jaccard 全在 0.13~0.24，正文一条都取不到，要点只能留空。改为「候选标题与
    同簇其它候选标题的字符覆盖率 ≥ 0.6」后，原始标题之间措辞天然接近，同事件
    0.97~1.00、不同事件 0.00~0.26，既捞回了漏掉的也拦得住混入的（见
    ``_title_cover`` / ``_candidate_in_event``）。

    对不上就返回空串（不喂正文、要点留空），宁缺勿错。正文和主标题重合
    （只是把标题重复一遍）的也当没有正文处理，免得白占 token。

    **主标题为空时不套标题匹配**：这类事件（钉钉作文、研报、周末汇总）本来就没有
    原标题，整簇就是这一件事，不存在「混进别条」的问题 —— 退回取最长的一条正文。
    早先这里一并要求标题匹配，导致 42 条待生成标题的事件全部拿不到正文，
    前 20 条覆盖率从 10/20 掉到 5/20。
    """
    name = _compact(str(event.get("name") or ""))
    rows = event.get("_candidates")
    best = ""
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        body = _body_from_text(name, text)
        if not body:
            continue
        if _body_repeats_title(name, body):
            continue
        body = _strip_repeated_lead(name, body)
        if not body:
            continue
        if not name:
            # 没有主标题可对齐：取最长的一条当代表（簇已按主体聚合）。
            if len(body) > len(best):
                best = body
            continue
        # 候选要讲的是本事件这件事，才认它的正文（见 _candidate_in_event）。
        if not _candidate_in_event(row, event):
            continue
        return body
    # 有正文但都对不上本事件：宁可不喂，也不要拿别人的正文编要点。
    return best


def _strip_repeated_lead(name: str, body: str) -> str:
    """剥掉正文开头与主标题重合的那一句。

    正文首句常常是标题的扩写（「财政部发布2026年上半年中…」），模型容易直接
    抄它当要点，产出等于标题+第一句的复述。把重合的首句摘掉，模型手里剩下的
    才是真正的新事实。只剥开头第一句，不动后面的句子。

    **首行开头是【标题】方括号时只剥方括号，不整行删掉**：财联社/新浪这类
    快讯正文常常是「【标题】正文……」挤在同一行（没有换行）。早先整行删，
    等于把正文一起删了 —— 结果「联合国秘书长谴责美方制裁国际刑事法院」
    这类事件正文取成空串，明明有一整段事实却写不出要点。方括号里就是标题的
    话，剥掉方括号，后面那段才是正文。
    """
    if not name:
        return body
    lines = body.splitlines()
    if not lines:
        return body
    first = _compact(lines[0])
    # 先处理「【标题】正文」同在一行的情况：方括号内容与主标题重合就只去括号。
    bracket = re.match(r"^\s*[【\[](?P<head>[^】\]]*)[】\]]\s*", lines[0])
    if bracket:
        head = _compact(bracket.group("head"))
        if head and (
            head == name
            or name in head
            or _headline_similarity(name, head) >= _SUBTITLE_SAME_TOPIC
        ):
            rest = _compact(lines[0][bracket.end():])
            # 括号后还有实打实的内容才认；只剩标点/空白就按整行重复处理。
            if rest and len(rest) >= 8:
                return "\n".join([lines[0][bracket.end():].strip(), *lines[1:]])
    # 首句和主标题高度重合（标题整句在里面，或字符重合率高）就整句摘掉。
    if name in first or _headline_similarity(name, first) >= _SUBTITLE_SAME_TOPIC:
        return "\n".join(lines[1:])
    return body


def _body_repeats_title(name: str, body: str) -> bool:
    """正文只是把主标题重复一遍（带省略号、加来源前缀都算）。"""
    if not name:
        return False
    flat = _compact(body)
    if not flat:
        return True
    if name in flat:
        # 主标题整句都在正文里，剩下的字不超过标题一小截，就是重复。
        # 常见的是后面挂个省略号、来源后缀（「IEA下调原油供需预期...」）。
        return len(flat) - len(name) <= max(3, len(name) // 5)
    return _headline_similarity(name, flat) >= _SUBTITLE_SAME_TOPIC


def _title_prompt(events: list[dict], *, retry: bool) -> str:
    blocks = []
    for event in events:
        # 主标题 + 正文。主标题是已经定稿的展示标题，正文里才有细节。早先只喂
        # 主标题（怕混进同簇别的标题）结果是「无米下锅」——模型只能把主标题
        # 换个说法，产出一堆无价值的重复句。正文按事件各自取，不跨条拼接，
        # 也就不会出现「拿别条标题当内容」的错配。
        primary = str(event.get("name") or "").strip()
        if not primary:
            headlines = [
                str(item).strip()
                for item in (event.get("headlines") or [])
                if str(item).strip()
            ]
            if not headlines:
                fallback = str(event.get("headline") or "").strip()
                headlines = [fallback] if fallback else []
            primary = " / ".join(headlines)
        body = _event_body(event)[:_BODY_IN_PROMPT_CHARS]
        # 输入里**不要**把已有的展示标题叫「主标题」——那会诱导模型把它原样抄进
        # 输出的 title 字段（实测「主标题：中信证券：用景气…」进去、title 出来
        # 还是「中信证券：用景气…」，随后被 _drop_repeated_subtitle 判为复读清掉，
        # 展示前 20 的覆盖率因此掉到个位数）。改叫「已有标题（已定稿，不要重复、
        # 不要回填）」，并单独说明 title 字段填的是补充要点。
        if body:
            blocks.append(
                f"{event.get('_batch_id')}. 已有标题（已定稿，不要重复、不要回填）："
                f"{primary}\n正文：{body}"
            )
        else:
            blocks.append(
                f"{event.get('_batch_id')}. 已有标题（已定稿，不要重复、不要回填）："
                f"{primary}\n正文：（无）"
            )
    retry_line = ""
    if retry:
        # 重写轮单独强调：这批是上一轮没通过的，多半是模型把主标题复读了、或直接
        # 留空。只要正文里还有主标题没提到的事实，就必须挑一个写出来。
        retry_line = (
            "上一轮要点不合格，请重写。注意：下面每条的主标题都已经定稿，你写的"
            "要点**必须**是主标题里没有的新事实（数字、金额、比例、日期、地点、"
            "原因、影响对象）。**正文里有这类事实就一定要写出来，不要留空，也不要"
            "把主标题换个说法充数**；正文确实只是重复主标题时才留空。\n"
        )
    names = "、".join(_dictionary_names())
    return (
        "下面每条是一件与资本市场有关的资讯，给出「已有标题」和正文。"
        "「已有标题」是已经定稿、要原样展示的标题，你**不要**把它抄进输出、"
        "也不要改一个字。"
        "你只为每一条写一句话**补充要点**，填在 JSON 的 title 字段里："
        "要点只能包含「已有标题」里没有的具体事实，"
        "也就是正文里的数字、金额、比例、日期、地点、原因、影响对象。"
        "一句话，不超过 40 个字，只挑 1~2 个最关键的新事实；"
        "不要把正文缩写一遍，不要罗列全部数字、全部出席人员或全部业务。"
        "主标题本身已经说清楚的，一个字都不要重复。"
        "**判断标准：先在正文里找「已有标题」没提到的数字、金额、比例、日期、地点、"
        "原因、影响对象，找到任意一个就写出来**——哪怕它看起来不够重磅，只要"
        "「已有标题」没有，就是该写的新事实。"
        "例：「已有标题」是「某公司拟收购某标的股权」，正文是「转让对价 3.16 亿元、"
        "标的主营光探测芯片、完成后纳入合并报表」，正文里的 3.16 亿元就是标题没有的"
        "事实，要点写「转让对价 3.16 亿元」；不能因为标题已经说了「收购」就留空。"
        "只有翻遍正文、确实找不出「已有标题」之外的任何事实（正文就是把标题重复"
        "一遍）时才留空。留空是下策，不是「没把握就留空」。"
        "绝对不要把「已有标题」换个说法当作要点：不要复述标题，不要给标题加「称」"
        "「表示」「建议」「将」「并」，也不要去掉标题的标点另写一句。"
        "不要直接抄正文的第一句话——那句话通常就是标题的扩写。"
        "只输出 JSON："
        '{"events":[{"id":"与输入编号相同","title":"","concepts":[],"category":"",'
        '"direction":"利好或利空","importance":"重大或重要或一般或琐碎"}]}。'
        "输入有几条，events 就有几条。id 必须原样抄写下面的编号，不要漏，不要改成从 0 起，也不要按数组顺序另编。"
        "不要把别的编号的内容写到这一条上。"
        "不要半句话，不要时间戳，不要【】，不要省略号，不要把词从中间截断。"
        "不要写早上好、各位或利空叙事。不要用「事件」当要点。"
        "不要带来源前缀。不要输出重点、点评或栏目标签。不要只写媒体名。"
        "不要把已经点名的公司换成「公司」。不要写观点更新或券商组名。"
        f"category 只能是：{'、'.join(CATEGORY_NAMES)}。"
        "importance 按分量：央行利率、国家级政策、战争制裁、大宗商品冲击、龙头公司重大事项是重大；"
        "寻常涨价、获批、订单是重要。"
        f"concepts 只在该事件还没有概念时填写，并且只能从这些名称里选：{names}。"
        "对不上就留空。不要新造概念。不要用来源栏目，栏目名里的快报不是概念。"
        "概念要和标题里的国家地区一致，不要把伊朗或中东标成俄乌。"
        "direction 表示对 A 股相关方向是利好还是利空。"
        "不要编造正文里没出现的事实。\n"
        f"{retry_line}\n"
        + "\n".join(blocks)
    )


def _request_titles(events: list[dict], *, retry: bool) -> str:
    from app.news.service import _llm_text

    try:
        return _llm_text(
            _title_prompt(events, retry=retry),
            max_tokens=_LLM_TITLE_TOKENS,
            prefer_final_answer=True,
            # 一批 40 条要点，每条还带正文，默认 30 秒不够（实测有整批返回空，
            # 疑似超时）。放宽到 90 秒，让模型有时间把整批写完。
            timeout=_LLM_TITLE_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("热门事件标题生成失败: %s", exc)
        return ""


def _label_with_llm(events: list[dict]) -> None:
    from app.news.config import llm_extract_enabled

    if not events or not llm_extract_enabled():
        return
    from app.news.service import reserve_hot_title_call

    _purge_stale_title_cache()
    pending: list[dict] = []
    for event in events:
        cache_key = _title_cache_key(str(event.get("_fp") or ""))
        cached = _LLM_CACHE.get(cache_key)
        if isinstance(cached, dict) and _cached_title_usable(cached):
            if not cached.get("_miss"):
                _apply_llm(event, cached)
            continue
        if cached is not None:
            _LLM_CACHE.pop(cache_key, None)
        pending.append(event)
    if not pending:
        return
    # 事件数经常上百条，一个批次装不下。按批循环问，每批各自编号，
    # 否则只有前一批有点要、后面的永远轮不到（实测 104 条只问了 20 条）。
    batches = [
        pending[start:start + _LLM_TITLE_BATCH]
        for start in range(0, len(pending), _LLM_TITLE_BATCH)
    ]
    for index, batch in enumerate(batches):
        # 额度要留给后面的批次：每批至少 1 次「首轮」调用，剩下的余量才允许本批
        # 做整批空 title 时的原地重发。否则前几批把额度吃光，后几批全成「未询问」
        # ——正是这一点让展示前 20 的覆盖率一度掉到 5/20。
        remaining = len(batches) - index - 1
        reserve = reserve_hot_title_call
        if remaining:
            reserve = _budget_guard(reserve_hot_title_call, remaining)
        _label_batch_with_llm(batch, reserve_call=reserve)


def _budget_guard(reserve_call, keep_for_later: int):
    """包一层额度函数：把后面 ``keep_for_later`` 批的首轮额度预留下来。

    本批的兜底（整批空 title 时的原地重发）只有在「发完这次，剩余额度仍够后面
    每批至少发一轮」时才放行。额度不够就返回 False，让本批走完首轮 + 重写轮了事。
    """

    def guarded() -> bool:
        from app.news.service import hot_title_calls_left

        if hot_title_calls_left() <= keep_for_later:
            return False
        return reserve_call()

    return guarded


def _looks_like_blank_sweep(
    batch: list[dict],
    parsed: dict[str, dict],
    no_body_ids: set[str],
) -> bool:
    """判断这一批是不是「整批 title 全空」的脏响应。

    成立条件：解析到了响应（不是超时/空响应），但**没有任何一条拿到非空 title**，
    且批内**有正文的条目占多数**（>=60%）。模型在正常工作时，对有正文的条目
    至少会写出几条要点（实测同批重发能拿到 14~18/20）；一条都写不出、而素材
    又都在，只能是上游偶发返回了一批空 title。

    注意别把「素材确实没话说」误判成脏响应：那种情况下 no_body_ids 会占多数，
    这里要求「有正文的是多数」，正好把这类真实留空排除在外。
    """
    if len(batch) < 4:
        return False  # 小批可能真的都没要点，不值得重发
    if not parsed:
        return False  # 没解析到响应走「整批无返回」那条路
    has_title = any(str(item.get("title") or "").strip() for item in parsed.values())
    if has_title:
        return False
    with_body = len(batch) - len(no_body_ids)
    return with_body * 5 >= len(batch) * 3


def _label_batch_with_llm(
    batch: list[dict],
    *,
    reserve_call,
) -> None:
    """问一批要点，没通过的再问一轮。batch 内的编号从 1 起。

    每批结束落一行分类统计，把「为什么这条没有要点」拆成四类：
    未询问（额度用满）/ 无正文（素材里没有新事实，模型本就不该写）/ 留空（模型
    返回空）/ 被丢（模型写了但没过门槛，附首要理由）。没有这行统计，线上只能看到
    「有要点的 0 条」，看不出是限流、无素材还是被规则拦掉。
    """
    if not batch:
        return
    for index, event in enumerate(batch, start=1):
        event["_batch_id"] = str(index)
    no_body_ids = {
        str(event.get("_batch_id")) for event in batch if not _event_body(event)
    }
    if not reserve_call():
        # 「额度用光」和「问了但没通过」是两回事，分开记，否则线上看不出真实通过率。
        logger.info(
            "热门事件要点本轮未询问: 额度已用满 (未询问 %d 条，其中无正文 %d 条)",
            len(batch), len(no_body_ids),
        )
        return
    first_raw = _request_titles(batch, retry=False)
    first = _parse_title_batch(first_raw, batch)
    # 整批一条都没解析出来，通常不是「模型全都留空」，而是响应空/被截断/超时。
    # 单独记一行，否则和「模型如实留空」混在一起，看不出真实通过率。
    if not first:
        logger.info(
            "热门事件要点整批无返回: %d 条，响应 %d 字符（疑似超时/截断）",
            len(batch), len(first_raw or ""),
        )
    # 上游 content 网关会偶发地把整批 title 都返成空 —— 正文都在，模型只回一个
    # 空 title 的骨架 JSON（或回满长度但 title 字段全空）。实测**同一批原文重发**
    # 就能恢复：固定一批 20 条连发 6 次，3 次拿到 20/20、3 次 0/20（成功率约 50%），
    # 且恢复与响应长度无关（3000 字符的长响应也可能是空 title）。
    #
    # 所以脏响应的正解是**原地重发**，最多 3 次：50% → 75% → 87.5% → >93%，平均
    # 只花 1.5~2 次调用。**不要拆半**：拆半后每一半各自只有 50% 成功率，实测花
    # 3~4 次调用才拿到 8~9/20（原地重发只用 1~2 次就 20/20）—— 既更贵又更差。
    # 唯一的例外是批本身就小（<8 条），重发几次还不行就认了。
    resend = 0
    while resend < _BLANK_RESEND and _looks_like_blank_sweep(batch, first, no_body_ids):
        if not reserve_call():
            logger.info("热门事件要点整批空 title，重发未进行: 额度已用满 (%d 条)", len(batch))
            break
        resend += 1
        logger.info("热门事件要点整批空 title，原地重发第 %d 次: %d 条", resend, len(batch))
        time.sleep(0.8)
        first_raw = _request_titles(batch, retry=False)
        first = _parse_title_batch(first_raw, batch)
    if _looks_like_blank_sweep(batch, first, no_body_ids):
        logger.info(
            "热门事件要点整批空 title，重发 %d 次仍未恢复: %d 条（上游持续降级）",
            resend, len(batch),
        )
    failed: list[dict] = []
    for event in batch:
        parsed = first.get(str(event.get("_batch_id")))
        if parsed is None:
            failed.append(event)
            continue
        if not _accept_title(event, parsed):
            failed.append(event)
    if not failed:
        _log_label_stats(len(batch), [], no_body_ids, {})
        return
    for index, event in enumerate(failed, start=1):
        event["_batch_id"] = str(index)
    second: dict[str, dict] = {}
    if reserve_call():
        second = _parse_title_batch(_request_titles(failed, retry=True), failed)
    else:
        logger.info("热门事件要点重试未进行: 额度已用满 (%d 条)", len(failed))
    # 第二轮仍没拿到的条目，以及拿到但没过门槛的条目，各自记下最终去向。
    settled: list[str] = []  # 最终没要点的条目（留空或被丢）
    reasons: dict[str, str] = {}
    # 模型**主动返回空**的条目单独记 body 长度：评审要看「无正文模型留空」和
    # 「有正文模型仍留空」各多少 —— 前者是设计如此（没有新事实就不该硬写），
    # 后者才是提示词或素材取用有问题，需要继续查。
    blank_with_body: list[str] = []
    for event in failed:
        batch_id = str(event.get("_batch_id"))
        parsed = second.get(batch_id)
        if parsed is None:
            # 两轮都没有这一条的返回。模型留空还是整条漏回，这里分不出来，算「留空」。
            logger.info("热门事件要点缺失: %s", batch_id)
            settled.append("blank")
            continue
        if not _accept_title(event, parsed):
            blank = dict(parsed)
            blank["title"] = ""
            _apply_llm(event, blank)
            _LLM_CACHE[_title_cache_key(str(event.get("_fp") or ""))] = {
                "_miss": True,
                "_miss_at": time.monotonic(),
            }
            if not str(parsed.get("title") or "").strip():
                # 模型主动留空。记一行带 body 长度，方便区分两类空。
                body = _event_body(event)
                settled.append("blank")
                if body:
                    blank_with_body.append(f"{batch_id}({len(body)}字)")
            else:
                settled.append("dropped")
            reasons[batch_id] = _subtitle_reject_reason(
                str(parsed.get("title") or ""), _event_source_text(event),
            )
    if blank_with_body:
        logger.info(
            "热门事件要点有正文仍留空: %d 条 %s（素材够但模型没写，需查提示词/取文）",
            len(blank_with_body), " ".join(blank_with_body[:10]),
        )
    _log_label_stats(len(batch), settled, no_body_ids, reasons)


def _log_label_stats(
    total: int,
    settled: list[str],
    no_body_ids: set[str],
    reasons: dict[str, str],
) -> None:
    """一行汇总：看得到通过率和被丢的首要理由。

    settled 是**最终没要点**的条目去向（留空/被丢），无正文另算 —— 如果一个条目
    既无正文又留空，它只该记一次（在 settled 里）。所以有要点 = 总数 - 最终没要点数，
    这样不会出现负值（早期按窗口数相减时出过一次 -2）。
    """
    no_point = len(settled)
    kept = max(0, total - no_point)
    blank = settled.count("blank")
    dropped = settled.count("dropped")
    top = ""
    if reasons:
        from collections import Counter

        head, count = Counter(reasons.values()).most_common(1)[0]
        top = f"，首要理由「{head}」×{count}"
    logger.info(
        "热门事件要点统计: 共 %d 条 → 有要点 %d / 留空 %d / 被丢 %d / 无正文 %d%s",
        total, kept, blank, dropped, len(no_body_ids), top,
    )


def _accept_title(event: dict, parsed: dict | None) -> bool:
    title = str((parsed or {}).get("title") or "")
    reason = _subtitle_reject_reason(title, _event_source_text(event))
    if reason:
        logger.info("热门事件标题未采用: %s（%s）", title or "∅", reason)
        return False
    _apply_llm(event, parsed)
    _LLM_CACHE[_title_cache_key(str(event.get("_fp") or ""))] = parsed
    return True


def _parse_title_batch(raw: str, events: list[dict]) -> dict[str, dict]:
    from app.news.extract import _llm_object

    payload = _llm_object(raw)
    if not payload:
        return {}
    rows = payload.get("events")
    if not isinstance(rows, list):
        keys = ("title", "concepts", "category", "direction", "importance", "stocks")
        rows = [payload] if len(events) == 1 and any(key in payload for key in keys) else []
    normalized = [row for row in rows if isinstance(row, dict)]
    found: dict[str, dict] = {}
    by_id = {str(event.get("_batch_id")): event for event in events}
    for row in normalized:
        batch_id = str(row.get("id") or "").strip()
        if not batch_id and len(events) == 1:
            batch_id = str(events[0].get("_batch_id") or "")
        event = by_id.get(batch_id)
        if event is None:
            continue
        parsed = _parse_llm_payload(row, _event_source_text(event))
        if parsed:
            found[batch_id] = parsed
    return found


def _parse_llm_payload(payload: dict, text: str) -> dict | None:
    title = _compact(str(payload.get("title") or ""))
    allowed = set(_dictionary_names())
    concepts = []
    concept_directions: dict[str, str] = {}
    for item in payload.get("concepts") or payload.get("sectors") or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            direct = str(item.get("direction") or "")
        else:
            name, direct = str(item or "").strip(), ""
        in_text = bool(name) and name in text and _fine_concept(name, name)
        if name and name not in concepts and (in_text or name in allowed):
            concepts.append(name)
            if direct in {"利好", "利空"}:
                concept_directions[name] = direct
    stocks = []
    for item in payload.get("stocks") or []:
        if isinstance(item, str):
            name, code, direct = item.strip(), "", ""
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            code = str(item.get("code") or item.get("symbol") or "").strip()
            direct = str(item.get("direction") or "")
        else:
            continue
        if (name and name in text) or (code and code in text):
            row = {"name": name, "code": code}
            if direct in {"利好", "利空"}:
                row["direction"] = direct
            stocks.append(row)
    category = str(payload.get("category") or "").strip()
    if category not in CATEGORY_NAMES:
        category = ""
    direction = str(payload.get("direction") or "").strip()
    if direction not in {"利好", "利空"}:
        direction = ""
    importance = str(payload.get("importance") or "").strip()
    if importance not in _IMPORTANCE_POINTS:
        importance = ""
    if not title and not concepts and not stocks and not category and not importance:
        return None
    return {
        "title": title,
        "category": category,
        "direction": direction,
        "importance": importance,
        "concepts": concepts[:_CONCEPT_LIMIT],
        "concept_directions": concept_directions,
        "stocks": stocks,
    }


def _apply_llm(event: dict, parsed: dict) -> None:
    # 记一笔「这条已经问过模型」。补问切片外条目时靠它区分「还没问过」和
    # 「问过但模型留空」——后者不该再问一遍（重问也是同样结果，白耗额度）。
    event["_llm_title_applied"] = True
    title = _compact(str(parsed.get("title") or ""))
    if _subtitle_reject_reason(title) == "":
        event["subtitle"] = title
    else:
        event["subtitle"] = ""
    anchor_name = str(event.get("name") or "")
    story_titles = [str(event.get("headline") or "")]
    story_titles.extend(str(item) for item in (event.get("headlines") or []))
    story = _story_texts(anchor_name, story_titles)
    story_blob = "\n".join(story) or anchor_name
    _lead, action, _obj = _parse_phrase(story_blob)
    category = str(parsed.get("category") or "")
    if _category_allowed(category, story_blob, action):
        event["category"] = category
    # 负面动作已经是利空时，模型不能改回利好。规则没锁死时仍采用模型给的方向。
    signal = _direction_signal(story_blob, action)
    hinted = str(parsed.get("direction") or "")
    if signal == "利空":
        event["direction"] = "利空"
    elif hinted in {"利好", "利空"}:
        event["direction"] = hinted
    elif signal:
        event["direction"] = signal
    importance = str(parsed.get("importance") or "")
    if importance == "琐碎":
        importance = "一般"
    if importance in _IMPORTANCE_POINTS:
        event["importance"] = importance
    had_concepts = any(str(name).strip() for name in (event.get("concepts") or []))
    if had_concepts:
        for name in parsed.get("concepts") or []:
            if (
                name not in event["concepts"]
                and len(event["concepts"]) < _CONCEPT_LIMIT
                and _concept_grounded(str(name), story_blob)
            ):
                event["concepts"].append(name)
        event["concepts"] = _merge_mapped_concepts(
            _relevant_concepts(
                Counter({str(name): 1 for name in event.get("concepts") or []}),
                story,
                _subject_set(anchor_name, []),
            ),
            story_blob,
        )
    else:
        allowed = set(_dictionary_names())
        picked: list[str] = []
        for name in parsed.get("concepts") or []:
            if name in allowed and name not in picked and _concept_grounded(str(name), story_blob):
                picked.append(str(name))
            if len(picked) >= _CONCEPT_LIMIT:
                break
        event["concepts"] = picked
    event["mapping"] = [
        {"name": name, "kind": "concept", "direction": event.get("direction") or "利好"}
        for name in event["concepts"]
    ]
    known = {item["key"] for item in event.get("mentioned_stocks") or []}
    known_names = {item["name"] for item in event.get("mentioned_stocks") or []}
    for stock in parsed.get("stocks") or []:
        code = str(stock.get("code") or "").strip()
        name = str(stock.get("name") or "").strip()
        key = code or name
        if not key or key in known or name in known_names:
            continue
        known.add(key)
        event["mentioned_stocks"].append({
            "key": key,
            "name": name or key,
            "mentions": 1,
            "source_count": 1,
            "direction": event.get("direction") or "利好",
        })
    _annotate_directions(event, story_blob)
