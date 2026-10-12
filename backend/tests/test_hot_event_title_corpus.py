"""展示门槛回归语料。#52 到 #57 的坏标题和好标题，加上词中间截断、寒暄和叙事。"""
from __future__ import annotations

from datetime import datetime

from app.market_time import CN_TZ
from app.news.hot_events import (
    _apply_llm,
    _boundary_reason,
    _cleaned_display_ok,
    _cleaned_headline,
    _clip_at_boundary,
    _display_clause_ok,
    _display_gate_reason,
    _display_original,
    _ensure_shown_name,
    _fallback_title_ok,
    _first_credible_title,
    _first_original_title,
    _headline_tier,
    _is_commentary,
    _keyword_title,
    _llm_title_ok,
    _llm_title_reject_reason,
    _original_reject_reason,
    _parse_title_batch,
    _resync_importance_score,
    _subtitle_reject_reason,
)
from app.news.service import get_store, reset_store_for_tests, top_hot_events

NOW = datetime(2026, 10, 10, 8, 30, tzinfo=CN_TZ)

# 不能上屏。模型标题和回退标题走同一道门槛。
_BAD = (
    "橡胶价格大涨超发布公告",
    "叠加昨夜美国针对制裁",
    "和之前结构性降息",
    "根据宏观经济运行出台务实管用的增量政",
    "事件",
    "可能还会有几次加息",
    "财政的小增量政策也出",
    "新增隐债】财政部发布20",
    "IEA下调原油供需预期...",
    "32财政部发布2026年上半年中",
    "橡胶价格大涨超60家轮胎企业集体涨价今年以来",
    "重点...",
    "TIM材料",
    "隔夜美股半导体硬件波动点评",
    "侧面佐证AI电源地位&能力",
    "持续关注1）Q3业绩增长强",
    "①Q3业绩增长强劲",
    "重申AI安全白宫《超级智能协议》签署",
    "Vicor对V",
    "PCB设备&钓针观点更新",
    "运作满一年产品近八",
    "Cybercab扩容提速",
    "某某公司发布新品天风计算机",
    "锂电行业更新-20261009#十五五政策定调",
    "风险可控从容应",
    "早上好",
    "各位",
    "各位投资者早上好",
    "利空叙事在节前已经释放",
    "光智科技",
    "央视财经",
    "美国在前财长宣布加息",
    "数据显示增长明显",
    "同比增长50%",
    "同比增长",
    "橡胶价格大涨至13",
    "结构性降息",
    "财政部发布2026年上半年中",
    "橡胶价格大涨超60家轮胎企业集体",
    "🌸固态电池",
    "高争民爆/西藏地矿/西藏铜矿资",
    "市场没有太关注到公司近期正在进行",
    "数据印证需求正在回暖",
    "板块利好继续扩散",
    "机构看好算力产业链",
    "继续关注AI视频板块",
    "建议关注算力",
    "推荐布局低空经济",
    "机构建议继续布局",
    "财政部发布",
    "招商电新称十五五政策定调固态电池产业化进入关键期",
    "招商电新称十五五…",
    "早上好考虑到",
    "早上好考虑到…",
    "早上好考虑到美联储降息",
    "腾讯&Meta对比",
    "Optimus加速落地",
    "中国巨石预告点评扣非表现好于预期（激励费环比增长）",
)

# 可以上屏。回退标题不要求宾语，所以「油价走高」在这里，不在模型验收里。
_GOOD = (
    "美联储降息25基点",
    "美联储宣布降息25基点",
    "美联储宣布降息二十五基点",
    "工信部出台机器人补贴",
    "存储芯片厂宣布涨价",
    "橡胶价格大涨",
    "橡胶价格大涨超预期",
    "橡胶价格大涨超60家轮胎企业集体涨价",
    "橡胶价格大涨超60%",
    "橡胶价格大涨超六成",
    "橡胶价格大涨至每吨13.1元",
    "油价走高",
    "美国制裁",
    "宏观经济运行情况出台务实管用的增量政策",
    "宏观经济运行出台务实管用的增量",
    "财政部发布2026年上半年中长期财政规划",
    "财政部发布2026年上半年中长期财政规划并明确后续增量政策",
    "财政部发布上半年财政规划",
    "财政部发布2025年一揽子化债方案",
    "IEA下调原油供需预期",
    "景旺电子Q3利润加速释放",
    "立讯精密及立讯技术涉337调查尚处立案初期",
    "立讯精密公告涉美国337调查",
    "建发股份与优必选战略合作签约",
    "建发股份与优必选签署战略合作",
    "赛力斯与宁德时代签署长期战略合作协议",
    "宁德时代回购巩固行业地位",
    "宁德时代发布固态电池",
    "宁德时代净利润同比增长50%",
    "宁德时代布局固态电池",
    "公司在前三季度净利润增长50%",
    "宁德时代在前十大股东中新增社保基金",
    "在前述公告基础上追加回购",
    "十五五政策定调",
    "十五五政策定调固态电池产业化进入关键期",
    "Vicor对VPD发起337调查",
    "某某公司发布新品",
    "特斯拉发布Cybercab扩容提速",
    "华为发布盘古新模型",
    "华为发布新模型",
    "航运公司上调运价",
    "碳酸锂报价继续上涨",
    "机构点评美联储降息",
    "证监会立案后机构建议回避",
    "国务院研究称将出台增量政策",
    "国务院布局未来产业",
    "工信部建议加快5G部署",
    "发改委布局算力枢纽",
    "国资委推荐央企试点",
    "文旅部出台演唱会补贴",
    "苹果发布智能家居新品",
    "苹果公司宣布将于10月13日在纽约发布全新iPhone",
    "黄金价格上涨",
    "欧洲多国电网在凌晨大范围熄灯",
    "沙特利雅得机场再次遭袭造成12人受伤",
    "沪指在前一交易日大涨后回调",
    "白宫签署超级智能协议",
    "优化实施财政金融协同促内需一揽子政策",
    "中国巨石预告扣非表现好于预期",
)


def test_display_gate_rejects_the_bad_corpus_and_keeps_the_good_one():
    """坏标题不能过门槛。好标题模型和回退都看这同一道。"""
    for title in _BAD:
        assert _display_gate_reason(title), title
        assert not _fallback_title_ok(title)
        assert not _llm_title_ok(title)
        assert not _cleaned_display_ok(title)
        assert not _display_clause_ok(title)
        assert _keyword_title([{"title": title}]) != title
        assert _clip_at_boundary(title) != title
    for title in _GOOD:
        assert _display_gate_reason(title) == "", title
        assert _fallback_title_ok(title)
        assert _cleaned_display_ok(title)


def test_cutoff_and_missing_party_lose_to_the_full_headline():
    """半截或丢掉合作方的标题，在簇里有完整原标题时不能上屏。"""
    apple = "苹果公司宣布将于10月13日在纽约发布"
    iphone = "苹果公司宣布将于10月13日在纽约发布全新iPhone"
    assert not _llm_title_ok(apple)
    assert _first_credible_title([("cls", apple), ("cls", iphone)]) == iphone

    short = "建发股份战略合作签约"
    paired = "建发股份与优必选战略合作签约"
    assert _display_gate_reason(short, [paired]) == "缺少合作方"
    assert _first_credible_title([("cls", short), ("reddit", paired)]) == paired

    plan = "十五五政策定调"
    full = "十五五政策定调固态电池产业化进入关键期"
    assert _display_gate_reason(plan) == ""
    assert _first_credible_title([("cls", plan), ("wscn", full)]) == full

    assert _first_credible_title([
        ("reddit", "Vicor对V"),
        ("cls", "Vicor对VPD发起337调查"),
    ]) == "Vicor对VPD发起337调查"


def test_failed_title_walks_headlines_by_source_credibility():
    """模型标题不合格时，按来源可信度试清理后的原标题。都不过就留空。"""
    event = {
        "name": "风险可控从容应",
        "_llm_title_applied": True,
        "headline": "早上好",
        "headlines": ["早上好"],
        "_candidates": [
            {"source": "reddit", "title": "美联储宣布降息25基点"},
            {"source": "cls", "title": "美联储降息25基点"},
        ],
    }
    _ensure_shown_name(event)
    assert event["name"] == "美联储降息25基点"
    assert "_llm_title_applied" not in event

    empty = {
        "name": "各位投资者早上好",
        "headline": "早上好",
        "headlines": ["早上好", "各位"],
        "_candidates": [
            {"source": "cls", "title": "早上好"},
            {"source": "wscn", "title": "各位投资者早上好"},
        ],
    }
    _ensure_shown_name(empty)
    assert empty["name"] == ""


def _quote_open_for_test(text: str) -> bool:
    stack: list[str] = []
    for char in text:
        if char == '"':
            if stack and stack[-1] == '"':
                stack.pop()
            else:
                stack.append('"')
    return bool(stack)


def _publish(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 9, hour, minute, tzinfo=CN_TZ)


def _insert(source_id: str, title: str, hour: int, minute: int, source: str = "cls", level: str = "", clean: str = "") -> None:
    status = get_store().insert_item(
        source=source,
        source_id=source_id,
        published_at=_publish(hour, minute),
        author="",
        title=title,
        clean_text=clean or title,
        raw=None,
        content_hash=source_id,
        url="",
        level=level,
        media_ids=[],
        extra=None,
        mentions=[],
    )
    assert status == "inserted"


def test_vicor_cutoff_mentions_merge_into_the_full_event(tmp_path):
    """四条 Vicor对V 超过 6 小时也要并回完整标题，提及次数留在完整那条上。"""
    reset_store_for_tests(tmp_path / "vicor-mentions.sqlite")
    _insert("v1", "Vicor对V", 0, 13)
    _insert("v2", "Vicor对V", 0, 40, source="wscn")
    _insert("v3", "Vicor对V", 1, 20, source="reuters")
    _insert("v4", "Vicor对V", 2, 0, source="bloomberg")
    _insert("v5", "Vicor对VPD发起337调查", 8, 39, source="cls")
    events = top_hot_events(NOW)["events"]
    names = [event["name"] for event in events]
    assert "Vicor对V" not in names
    vicor = [event for event in events if event["name"] == "Vicor对VPD发起337调查"]
    assert len(vicor) == 1
    assert vicor[0]["mentions"] == 5


def test_red_chatter_and_narrative_are_dropped(tmp_path):
    """加红也不能把寒暄、叙事和词中间截断放上榜。"""
    reset_store_for_tests(tmp_path / "chatter.sqlite")
    _insert("greet", "各位投资者早上好", 10, 1, level="A")
    _insert("story", "利空叙事在节前已经释放", 10, 2, source="wscn", level="A")
    _insert("cut", "风险可控从容应", 10, 3, source="reuters", level="A")
    _insert("fed", "美联储降息25基点", 10, 4)
    names = [event["name"] for event in top_hot_events(NOW)["events"]]
    assert "各位投资者早上好" not in names
    assert "利空叙事在节前已经释放" not in names
    assert "风险可控从容应" not in names
    assert "美联储降息25基点" in names


def test_bare_publish_loses_to_the_full_policy_line(tmp_path):
    """只有「财政部发布」时不上屏，簇里有完整政策标题就用那一条。"""
    reset_store_for_tests(tmp_path / "fiscal-object.sqlite")
    short = "财政部发布"
    full = "优化实施财政金融协同促内需一揽子政策"
    _insert("short", short, 10, 1)
    _insert("full", full, 10, 6, source="wscn")
    names = [event["name"] for event in top_hot_events(NOW)["events"]]
    assert short not in names
    assert full in names
    rows = [("cls", short), ("wscn", full)]
    assert _first_credible_title(rows) == full


def test_source_fallback_strips_the_broker_prefix():
    """回退到原标题时也要去掉招商电新称，不能把券商前缀再放上屏。"""
    raw = "招商电新称十五五政策定调固态电池产业化进入关键期"
    cleaned = "十五五政策定调固态电池产业化进入关键期"
    assert _cleaned_headline(raw) == cleaned
    assert _display_gate_reason(cleaned) == ""
    event = {
        "name": raw,
        "headline": raw,
        "headlines": [raw],
        "_candidates": [{"source": "cls", "title": raw}],
    }
    _ensure_shown_name(event)
    assert event["name"] == cleaned
    ellipsis = "招商电新称十五五…"
    assert _display_gate_reason(ellipsis)
    assert not _cleaned_headline(ellipsis).startswith("招商")


def test_original_headline_keeps_the_long_source_line():
    """主标题是清理后的原标题。要点失败不会把它换成短改写。"""
    assert _headline_tier("sec", "宁德时代公告发布固态电池新品") < _headline_tier(
        "cls", "新华社：财政部发布2026年上半年中长期财政规划",
    )
    assert _headline_tier("cls", "新华社：财政部发布2026年上半年中长期财政规划") < _headline_tier(
        "cls", "美联储降息25基点",
    )
    assert _headline_tier("cls", "美联储降息25基点") < _headline_tier("reuters", "美联储宣布降息25基点")
    assert _headline_tier("reuters", "美联储宣布降息25基点") < _headline_tier("ima", "美联储宣布降息25基点")
    assert _first_credible_title([
        ("ima", "群里说橡胶价格大涨超预期发布公告"),
        ("cls", "橡胶价格大涨超预期发布公告"),
        ("sec", "宁德时代公告发布固态电池新品"),
    ]) == "宁德时代公告发布固态电池新品"
    assert _display_original("央视财经：橡胶价格大涨超预期发布公告") == "橡胶价格大涨超预期发布公告"
    assert _display_original("【GLMS通信】橡胶价格大涨超预期发布公告") == "橡胶价格大涨超预期发布公告"
    assert _display_original("光智科技：公司发布新品") == "光智科技发布新品"
    battery = _display_original("宁德时代：发布固态电池、换电")
    assert battery == "宁德时代发布固态电池、换电"
    assert "固态电池、换电" in battery
    assert "固态电池，换电" not in battery
    assert _display_original("某某公司发布新品天风计算机") == "某某公司发布新品"
    long = "橡胶价格大涨超60家轮胎企业集体涨价，" + "后续多家企业继续跟进并上调出厂报价以及经销商报价"
    shown = _display_original(long)
    assert len(long) > 40
    assert shown == long
    assert "后续" in shown
    event = {
        "name": "短改写",
        "headline": "央视财经：橡胶价格大涨超预期发布公告",
        "headlines": ["央视财经：橡胶价格大涨超预期发布公告"],
        "_candidates": [{"source": "cls", "title": "央视财经：橡胶价格大涨超预期发布公告"}],
    }
    _apply_llm(event, {"title": "早上好"})
    assert event["subtitle"] == ""
    assert event["name"] == "短改写"
    _apply_llm(event, {"title": "橡胶价格大涨超预期"})
    assert event["subtitle"] == "橡胶价格大涨超预期"
    assert event["name"] == "短改写"
    _apply_llm(event, {"title": "美联储宣布降息二十五基点"})
    assert event["subtitle"] == "美联储宣布降息二十五基点"
    assert event["name"] == "短改写"
    _ensure_shown_name(event)
    assert event["name"] == "橡胶价格大涨超预期发布公告"
    assert event["subtitle"] == "美联储宣布降息二十五基点"


def test_live_sentences_subtitles_and_research_chatter():
    """线上这批：要点留下，第一句不截断，转述和研报聊天不当主标题。"""
    kept = (
        "WTI原油期货收涨至91.85美元",
        "财政部实施更加积极财政政策",
        "纽约联储称特朗普关税推高商品价格",
        "苹果iPhoneDuo加单30%",
        "英伟达洽谈收购AI初创Reflection",
    )
    for line in kept:
        assert _subtitle_reject_reason(line) == "", line
    assert _subtitle_reject_reason("") == "空标题"
    assert _subtitle_reject_reason("早上好") == "寒暄"
    assert _subtitle_reject_reason("各位投资者请看") == "寒暄"
    assert _subtitle_reject_reason("财联社：美联储降息") == "来源前缀"
    assert _subtitle_reject_reason("叠加昨夜美国针对制裁") == ""
    live_subtitles = (
        "WTI11月原油期货收涨0.36美元报91.85美元/桶",
        "苹果iPhoneDuo加单30%，iPhone18Pro系列总订单量未变",
        "特朗普与普京达成的柴油协议被指违反俄罗斯制裁法",
    )
    for line in live_subtitles:
        assert _subtitle_reject_reason(line) == "", line
    assert _subtitle_reject_reason("\ufffd") == "乱码"
    assert _subtitle_reject_reason("锟斤拷") == "乱码"
    assert _subtitle_reject_reason("Ã¤Â¸Â­文") == "乱码"
    assert _subtitle_reject_reason("!!!@@@###") == "乱码"

    price = "统一涨价10%！光刻机巨头ASML将上调服务价格"
    shown_price = _display_original(price)
    assert "统一涨价10%" in shown_price
    assert "ASML将上调" in shown_price
    assert shown_price != "统一涨价10%"
    assert _display_original("华为发布盘古新模型！昇腾链走强") == "华为发布盘古新模型"

    quoted = '深圳公积金拟新增"装修住房提取。额度上调"'
    shown_quote = _display_original(quoted)
    assert "额度上调" in shown_quote
    assert shown_quote != '深圳公积金拟新增"装修住房提取'
    assert not _quote_open_for_test(shown_quote)
    closed = '深圳公积金拟新增"装修住房提取"'
    assert _display_original(closed) == closed

    broker = "招商电新锂电行业更新称十五五政策定调固态电池产业化进入关键期"
    assert _display_original(broker) == "十五五政策定调固态电池产业化进入关键期"
    assert _display_original("经济日报深圳公积金拟新增装修住房提取额度") == "深圳公积金拟新增装修住房提取额度"
    assert _display_original("经济日报：深圳公积金拟新增装修住房提取额度") == "深圳公积金拟新增装修住房提取额度"
    assert _display_original("经济日报") == ""

    demand = "国家发改委出台一揽子增量政策更好鼓励居民消费、撬动社会投资并扩大有效需求持续释放内需潜力"
    industry = "国家把十五五政策定调固态电池产业化进入关键期并带动上下游设备与材料订单集中释放落地"
    plan = '多部门共同推动"十五五"规划落地并明确未来五年的产业支持方向、配套资金安排和年度项目清单'
    ai4s = "AI4S产业加速从实验室走向中试并带动科研仪器与算力需求以及交叉学科平台持续建设落地"
    for sentence in (demand, industry, plan, ai4s):
        assert len(sentence) > 40
        assert _display_original(sentence) == sentence
    assert _display_original(demand).endswith("撬动") is False
    assert "产业化进入关键期" in _display_original(industry)
    assert _display_original(plan) != '推动"十五五"规划'
    assert _display_original(ai4s) != "AI4S产业"

    nvidia = "英伟达洽谈收购AI初创Reflection"
    assert _display_original("报道" + nvidia) == nvidia
    assert _display_original("据报道，" + nvidia) == nvidia
    assert _display_original("消息称" + nvidia) == nvidia

    clinical = "J26完成RCT临床验证1基本面更新"
    pcb = "小PCB公司们的8月单月业绩崇达8月单月1e+利润，超声"
    tip = "调整建议把握机会！"
    assert _first_credible_title([("zsxq", clinical), ("cls", nvidia)]) == nvidia
    assert _first_credible_title([("ima", clinical)]) == ""
    assert _first_credible_title([("dws", pcb)]) == ""
    assert _first_credible_title([("cls", tip), ("wscn", nvidia)]) == nvidia
    assert _first_credible_title([("cls", tip)]) == ""

    chat = {
        "name": "旧",
        "headline": clinical,
        "headlines": [clinical],
        "_candidates": [{"source": "zsxq", "title": clinical}],
        "subtitle": nvidia,
    }
    _ensure_shown_name(chat)
    assert chat["name"] == nvidia
    assert chat["subtitle"] == ""
    assert _llm_title_reject_reason(nvidia) == ""

    weak = {
        "name": "旧",
        "headline": clinical,
        "headlines": [clinical],
        "_candidates": [{"source": "ima", "title": clinical}],
        "subtitle": "WTI原油期货收涨至91.85美元",
    }
    _ensure_shown_name(weak)
    assert weak["name"] == ""


def test_realistic_batch_keeps_subtitles(tmp_path, monkeypatch):
    """线上这三条要点含数字、拉丁、百分号和斜杠，一批下来不能是空的。"""
    import json

    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    reset_store_for_tests(tmp_path / "subtitle-batch.sqlite")
    # (source_id, 主标题, 期望要点, 正文)。线上 clean_text 里有正文，要点引用的
    # 数字（11月、0.36 美元、30%）都来自正文。这里必须带上正文，否则「要点事实
    # 必须出自素材」这道核对会把正确要点判成编造 —— 那正是要防的误杀。
    samples = (
        (
            "wti",
            "WTI原油期货上涨报91.85美元",
            "WTI11月原油期货收涨0.36美元报91.85美元/桶",
            "WTI11月原油期货收涨0.36美元报91.85美元/桶，市场关注供需变化。",
        ),
        (
            "apple",
            "苹果宣布追加iPhone订单",
            "苹果iPhoneDuo加单30%，iPhone18Pro系列总订单量未变",
            "苹果iPhoneDuo加单30%，iPhone18Pro系列总订单量未变。",
        ),
        (
            "diesel",
            "财政部宣布柴油协议触发制裁",
            "特朗普与普京达成的柴油协议被指违反俄罗斯制裁法",
            "特朗普与普京达成的柴油协议被指违反俄罗斯制裁法。",
        ),
    )
    for index, (source_id, title, _subtitle, body) in enumerate(samples):
        _insert(source_id, title, 9, 10 + index, clean=body)
        _insert(source_id + "-b", title, 9, 20 + index, source="wscn", clean=body)

    def fake(prompt: str, **_kwargs: object) -> str:
        rows = []
        for line in prompt.splitlines():
            head, sep, rest = line.partition(". ")
            if not sep or not head.isdigit() or not rest.strip():
                continue
            title = ""
            if "原油" in rest:
                title = samples[0][2]
            elif "苹果" in rest or "iPhone" in rest:
                title = samples[1][2]
            elif "制裁" in rest or "柴油" in rest:
                title = samples[2][2]
            rows.append({"id": head, "title": title, "importance": "重要"})
        return json.dumps({"events": rows}, ensure_ascii=False)

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", fake)
    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    events = top_hot_events(NOW)["events"]
    subtitles = [str(event.get("subtitle") or "") for event in events]
    assert events
    assert all(subtitles)
    for _source_id, _title, subtitle, _body in samples:
        assert subtitle in subtitles


def test_subjectless_media_headline_uses_the_next_source():
    """去掉报名后没有主体的口号，改用下一条来源，不把「经济日报评论」留在主标题上。"""
    slogan = "经济日报评论：培育赛事经济新增长极"
    nxt = "财政部出台赛事经济扶持政策"
    assert _display_original("经济日报：培育赛事经济新增长极") == "培育赛事经济新增长极"
    assert _original_reject_reason("培育赛事经济新增长极") == "缺少主体或动作"
    assert _first_original_title([("cls", slogan), ("wscn", nxt)]) == nxt
    assert _first_original_title([("cls", slogan)]) == ""


def test_subtitle_is_keyed_by_event_id_not_list_order():
    """模型把 WTI 写在数组前面时，仍按 id 对上，不能安到纳斯达克那条上。"""
    nasdaq = "纳斯达克金龙中国指数上涨"
    oil = "WTI原油期货上涨报91.85美元"
    events = [
        {"_batch_id": "1", "name": nasdaq, "headline": nasdaq, "headlines": [nasdaq]},
        {"_batch_id": "2", "name": oil, "headline": oil, "headlines": [oil]},
    ]
    swapped = (
        '{"events":['
        '{"id":"2","title":"WTI原油期货收涨至91.85美元"},'
        '{"id":"1","title":"纳斯达克金龙中国指数收涨"}'
        ']}'
    )
    parsed = _parse_title_batch(swapped, events)
    assert parsed["1"]["title"] == "纳斯达克金龙中国指数收涨"
    assert parsed["2"]["title"] == "WTI原油期货收涨至91.85美元"
    assert "WTI" not in parsed["1"]["title"]
    shifted = (
        '{"events":['
        '{"title":"WTI原油期货收涨至91.85美元"},'
        '{"title":"纳斯达克金龙中国指数收涨"}'
        ']}'
    )
    assert _parse_title_batch(shifted, events) == {}


def test_subtitle_repeats_are_dropped_and_listing_keeps_the_field(tmp_path, monkeypatch):
    """要点和主标题重复就留空。页面接口要把剩下的要点带出去。漏掉的编号不要记成永久未采用。"""
    import json

    from app.news import hot_events as hot_mod
    from app.news import service as news_service
    from app.news.service import hot_event_listing

    same = {
        "name": "航运公司上调运价",
        "headline": "航运公司上调运价",
        "headlines": ["航运公司上调运价"],
        "_candidates": [{"source": "cls", "title": "航运公司上调运价"}],
        "subtitle": "上调运价",
    }
    _ensure_shown_name(same)
    assert same["name"] == "航运公司上调运价"
    assert same["subtitle"] == ""
    distinct = {
        "name": "旧",
        "headline": "航运公司上调运价",
        "headlines": ["航运公司上调运价"],
        "_candidates": [{"source": "cls", "title": "航运公司上调运价"}],
        "subtitle": "运价上调影响集运公司",
    }
    _ensure_shown_name(distinct)
    assert distinct["subtitle"] == "运价上调影响集运公司"

    reset_store_for_tests(tmp_path / "subtitle-id.sqlite")
    oil = "WTI原油期货上涨报91.85美元"
    nasdaq = "纳斯达克金龙中国指数上涨"
    _insert("oil", oil, 9, 10)
    _insert("oil-b", oil, 9, 12, source="wscn")
    _insert("nasdaq", nasdaq, 9, 11)
    _insert("nasdaq-b", nasdaq, 9, 13, source="wscn")
    prompts: list[str] = []
    flags: list[bool] = []

    def first(prompt: str, **kwargs: object) -> str:
        prompts.append(prompt)
        flags.append(bool(kwargs.get("prefer_final_answer")))
        rows = []
        for line in prompt.splitlines():
            head, sep, rest = line.partition(". ")
            if not sep or not head.isdigit() or not rest.strip():
                continue
            if "原油" in rest:
                rows.append({"id": head, "title": "WTI原油期货收涨至91.85美元"})
            elif "纳斯达克" in rest:
                rows.append({"id": head, "title": "纳斯达克金龙中国指数收涨"})
        rows.reverse()
        return json.dumps({"events": rows}, ensure_ascii=False)

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", first)
    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    listed = hot_event_listing(NOW)
    by_name = {event["name"]: event for event in listed["events"]}
    assert by_name[oil]["subtitle"] == "WTI原油期货收涨至91.85美元"
    assert by_name[nasdaq]["subtitle"] == "纳斯达克金龙中国指数收涨"
    assert "WTI" not in by_name[nasdaq]["subtitle"]
    assert flags and all(flags)
    assert hot_mod._LLM_TITLE_TOKENS >= 4096
    assert not any(item.get("_miss") for item in hot_mod._LLM_CACHE.values() if isinstance(item, dict))


def test_omitted_subtitle_row_is_not_cached_as_miss(tmp_path, monkeypatch):
    """模型没返回某个编号时，不要记成未采用，下一轮还能补上要点。"""
    import json
    from datetime import timedelta

    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    reset_store_for_tests(tmp_path / "subtitle-miss.sqlite")
    oil = "WTI原油期货上涨报91.85美元"
    nasdaq = "纳斯达克金龙中国指数上涨"
    _insert("oil", oil, 9, 10)
    _insert("oil-b", oil, 9, 12, source="wscn")
    _insert("nasdaq", nasdaq, 9, 11)
    _insert("nasdaq-b", nasdaq, 9, 13, source="wscn")

    def partial(prompt: str, **_kwargs: object) -> str:
        if "上一轮" in prompt:
            return "{}"
        rows = []
        for line in prompt.splitlines():
            head, sep, rest = line.partition(". ")
            if not sep or not head.isdigit() or "原油" not in rest:
                continue
            rows.append({"id": head, "title": "WTI原油期货收涨至91.85美元"})
        return json.dumps({"events": rows}, ensure_ascii=False)

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", partial)
    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    first = {event["name"]: event for event in top_hot_events(NOW)["events"]}
    assert first[oil]["subtitle"] == "WTI原油期货收涨至91.85美元"
    assert first[nasdaq]["subtitle"] == ""
    assert not any(item.get("_miss") for item in hot_mod._LLM_CACHE.values() if isinstance(item, dict))

    def complete(prompt: str, **_kwargs: object) -> str:
        rows = []
        for line in prompt.splitlines():
            head, sep, rest = line.partition(". ")
            if not sep or not head.isdigit() or not rest.strip():
                continue
            if "纳斯达克" in rest:
                rows.append({"id": head, "title": "纳斯达克金龙中国指数收涨"})
            elif "原油" in rest:
                rows.append({"id": head, "title": "WTI原油期货收涨至91.85美元"})
        return json.dumps({"events": rows}, ensure_ascii=False)

    monkeypatch.setattr(news_service, "_llm_text", complete)
    hot_mod.clear_hot_event_cache(llm=False)
    second = {event["name"]: event for event in top_hot_events(NOW + timedelta(minutes=11))["events"]}
    assert second[nasdaq]["subtitle"] == "纳斯达克金龙中国指数收涨"


def test_same_company_earnings_notes_are_one_event(tmp_path):
    """同一家公司的业绩预告并成一件。主标题用更可信来源的完整原标题。"""
    reset_store_for_tests(tmp_path / "jushi.sqlite")
    raw = "中国巨石预告点评扣非表现好于预期（激励费环比增长）"
    other = "中国巨石扣非净利润同比增长"
    assert _cleaned_headline(raw) == "中国巨石预告扣非表现好于预期"
    _insert("note", raw, 9, 10)
    _insert("result", other, 11, 40, source="wscn")
    events = [event for event in top_hot_events(NOW)["events"] if "巨石" in event["name"]]
    assert len(events) == 1
    assert events[0]["mentions"] == 2
    assert events[0]["name"] == raw


def test_event_body_uses_article_text_and_skips_title_repeat():
    """正文取事件里的文章正文；和主标题重复的当没有正文。"""
    from app.news.hot_events import _event_body

    event = {
        "name": "粤芯半导体将于10月13日在创业板上市",
        "_candidates": [
            {
                "source": "cls",
                "title": "粤芯半导体：将于10月13日在创业板上市",
                "text": (
                    "粤芯半导体：将于10月13日在创业板上市\n"
                    "发行价12.01元/股，发行后总股本28.78亿股，公开发行5.13亿股。"
                ),
            },
        ],
    }
    body = _event_body(event)
    assert "12.01元/股" in body
    assert "28.78亿股" in body
    # 正文行首的标题行要剥掉。
    assert not body.startswith("粤芯半导体：")

    # 正文只是把主标题重复一遍（带省略号），等于没有正文。
    repeated = {
        "name": "IEA下调原油供需预期",
        "_candidates": [{"source": "cls", "title": "IEA下调原油供需预期", "text": "IEA下调原油供需预期\nIEA下调原油供需预期..."}],
    }
    assert _event_body(repeated) == ""


def test_event_body_strips_leading_sentence_that_repeats_title():
    """正文首句是主标题的扩写时摘掉，避免模型拿它当要点复述。"""
    from app.news.hot_events import _event_body

    event = {
        "name": "财政部表示有力有效实施更加积极的财政政策",
        "_candidates": [
            {
                "source": "cls",
                "title": "财政部表示有力有效实施更加积极的财政政策",
                "text": (
                    "财政部表示有力有效实施更加积极的财政政策\n"
                    "财政部表示有力有效实施更加积极的财政政策并谋划出台增量政策。\n"
                    "上半年新增减税降费及退税缓费 1.6 万亿元。"
                ),
            },
        ],
    }
    body = _event_body(event)
    # 与主标题重合的首句被摘掉，剩下的是真正的新事实。
    assert "1.6 万亿元" in body
    assert body.count("有力有效") == 0


def test_event_body_ignores_candidate_that_talks_about_something_else():
    """主标题有值时，簇里混进别条新闻不能取它的正文 —— 会把要点带偏成别人的事实。

    实测错配：「中信证券：用景气和供给出清来应对高利率…」的要点写成了
    「沃勒预计还需加息两次」，那句来自同簇另一条「美联储理事沃勒表示…」的正文。
    主标题有值、却和所有候选标题都对不上时，宁可返回空（要点留空），
    也不要拿别人的正文编。
    """
    from app.news.hot_events import _event_body

    event = {
        "name": "中信证券：用景气和供给出清来应对高利率",
        "_candidates": [
            {
                "source": "cls",
                "title": "中信证券：用景气和供给出清来应对高利率",
                # 只有标题行，没有正文。
                "text": "中信证券：用景气和供给出清来应对高利率",
            },
            {
                "source": "wscn",
                "title": "美联储理事沃勒表示仍需加息两次",
                # 同簇另一条新闻，正文里有「沃勒」这个不属于主标题的事实。
                "text": (
                    "美联储理事沃勒表示仍需加息两次\n"
                    "沃勒预计还需加息两次至 4.25-4.50% 区间，以压制通胀。"
                ),
            },
        ],
    }
    # 有正文的那条标题讲的是沃勒，与中信这条主标题对不上，不取。
    assert _event_body(event) == ""


def test_event_body_falls_back_to_longest_when_no_main_title():
    """主标题为空时不套标题匹配，退回取最长正文 —— 这类事件整簇就是一件事。

    钉钉作文、研报、周末汇总在生成标题前 `name` 是空的。早先一并要求标题匹配，
    导致这些事件全部拿不到正文（实测 42 条，前 20 条覆盖率从 10/20 掉到 5/20）。
    """
    from app.news.hot_events import _event_body

    event = {
        "name": "",
        "_candidates": [
            {
                "source": "zsxq",
                "title": "【华源公用】山东核电机制电价比例提升",
                "text": "【华源公用】山东核电机制电价比例提升\n事件：据了解，从9月份起机制电价比例提升。",
            },
            {
                "source": "zsxq",
                "title": "【华源公用】山东核电机制电价比例提升，建议对核电保持乐观",
                "text": (
                    "【华源公用】山东核电机制电价比例提升，建议对核电保持乐观\n"
                    "事件：据了解，从9月份起机制电价比例提升，建议对核电保持乐观。"
                ),
            },
        ],
    }
    body = _event_body(event)
    assert body  # 拿得到正文
    assert "机制电价" in body


def test_event_body_accepts_candidate_whose_title_matches_main():
    """候选标题和主标题对得上时照常取正文（别把正确候选也挡掉）。"""
    from app.news.hot_events import _event_body

    event = {
        "name": "贵州茅台宣布回购",
        "_candidates": [
            {
                "source": "cls",
                "title": "贵州茅台宣布回购并注销股份",
                "text": (
                    "贵州茅台宣布回购并注销股份\n"
                    "拟回购金额不低于 30 亿元，回购价上限 1800 元/股。"
                ),
            },
        ],
    }
    body = _event_body(event)
    assert "30 亿元" in body


def test_subtitle_length_is_capped():
    """补充要点是「一行挑 1~2 个事实」，超长直接丢，不做截断。"""
    from app.news.hot_events import _SUBTITLE_MAX, _subtitle_reject_reason

    long_text = "拟以3.16亿元收购光探测芯片公司52.67%股权，标的去年营收8.2亿元净利润0.6亿元主营光模块封装测试业务"
    assert len(long_text) > _SUBTITLE_MAX
    assert "过长" in _subtitle_reject_reason(long_text, long_text)
    short = "拟3.16亿元收购快灵光电52.67%股权"
    assert _subtitle_reject_reason(short, short) == ""


def test_subtitle_facts_must_come_from_source():
    """要点里的数字和机构名必须能在正文/标题里找到，编造的丢掉。"""
    from app.news.hot_events import _subtitle_reject_reason

    source = "公司发布业绩公告\n公司上半年归母净利润12.3亿元，同比增长40%。"
    # 数字来自正文 → 通过。
    assert _subtitle_reject_reason("上半年归母净利润12.3亿元", source) == ""
    # 编造的数字（正文里没有）→ 丢。
    assert "数字" in _subtitle_reject_reason("上半年归母净利润15.8亿元", source)
    # 没有来源可核对时不拦（缓存路径拿不到 source）。
    assert _subtitle_reject_reason("上半年归母净利润15.8亿元") == ""


def test_subtitle_entity_check_is_case_insensitive():
    """拉丁实体大小写不同不算编造（iPhone/Pro 不能被拆成碎片误杀）。"""
    from app.news.hot_events import _subtitle_reject_reason

    source = "苹果宣布追加iPhone订单\niPhone18Pro系列总订单量未变，Duo加单30%。"
    subtitle = "iPhoneDuo加单30%，iPhone18Pro系列总订单量未变"
    assert _subtitle_reject_reason(subtitle, source) == ""


def test_title_prompt_feeds_article_body_and_asks_for_gap_filling():
    """提示词里带上各事件的正文，并要求只写主标题没有的事实。"""
    from app.news.hot_events import _title_prompt

    events = [
        {
            "_batch_id": "1",
            "name": "宁德时代与赛力斯签署长期战略合作协议",
            "_candidates": [
                {
                    "source": "cls",
                    "title": "宁德时代与赛力斯签署长期战略合作协议",
                    "text": (
                        "宁德时代与赛力斯签署长期战略合作协议\n"
                        "签约地点为重庆，问界全系车型累计交付已突破120万台。"
                    ),
                },
            ],
        },
    ]
    prompt = _title_prompt(events, retry=False)
    assert "补充要点" in prompt
    # 已有标题这一行**不能**叫「主标题」——那会诱导模型把它抄进输出 title 字段，
    # 随后被判为复读清掉（展示前 20 覆盖率因此掉到个位数）。
    assert "1. 已有标题（已定稿，不要重复、不要回填）：宁德时代与赛力斯签署长期战略合作协议" in prompt
    assert "正文：签约地点为重庆" in prompt
    assert "120万台" in prompt
    # 明确禁止把已有标题换个说法当要点。
    assert "不要复述标题" in prompt
    # 长度约束写进 prompt（要点是一行，不超过 40 字）。
    assert "不超过 40 个字" in prompt
    # 「找到任意一个新事实就写出来」的正向指令必须在（评审要求：留空是下策）。
    assert "找到任意一个就写出来" in prompt
    # 没有正文的事件退回只给已有标题，并标注「（无）」。
    bare = _title_prompt([{"_batch_id": "1", "name": "某公司公告"}], retry=False)
    assert "1. 已有标题（已定稿，不要重复、不要回填）：某公司公告" in bare
    assert "正文：（无）" in bare


def test_label_stats_logs_zero_because_of_what(caplog):
    """分类统计要能把「为什么没要点」拆开：额度用满时记未询问、无正文单列。"""
    import logging

    from app.news.hot_events import _label_batch_with_llm

    batch = [
        {"name": "有正文", "_fp": "a", "_candidates": [
            {"source": "cls", "title": "有正文", "text": "有正文\n正文里有 12.3 亿元这个事实。"},
        ]},
        {"name": "无正文", "_fp": "b", "_candidates": [
            {"source": "cls", "title": "无正文", "text": "无正文"},
        ]},
    ]
    with caplog.at_level(logging.INFO, logger="app.news.hot_events"):
        # 额度用满：不该问模型，日志里要区分「未询问」和「无正文」。
        _label_batch_with_llm(batch, reserve_call=lambda: False)
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "未询问" in text
    assert "无正文 1 条" in text


def test_event_body_keeps_text_after_bracketed_title_on_same_line():
    """「【标题】正文…」挤在同一行时，只剥方括号、保留后面的正文。

    财联社/新浪快讯常见这种排版：标题和正文之间没有换行。早先整行删掉，等于把
    正文一起删了 —— 「联合国秘书长谴责美方制裁国际刑事法院」这类事件明明有一
    整段事实（古特雷斯声明、八国外长联合声明），取文却成空串，要点永远写不出。
    """
    from app.news.hot_events import _event_body

    name = "联合国秘书长谴责美方制裁国际刑事法院"
    event = {
        "name": name,
        "_candidates": [{
            "source": "cls",
            "title": name,
            "text": (
                f"{name}\n"
                f"【{name}】财联社10月10日电，联合国秘书长古特雷斯9日通过发言人"
                "发表声明，谴责美国宣布“对国际刑事法院整个机构实施制裁”。"
            ),
        }],
    }
    body = _event_body(event)
    assert body
    assert "古特雷斯" in body
    # 方括号（含标题）被剥掉，不会再出现在正文开头。
    assert not body.startswith(f"【{name}】")


def test_event_body_drops_bracket_only_when_title_repeats_without_content():
    """方括号里是标题、括号外没有实义内容时，仍按重复处理返回空串。"""
    from app.news.hot_events import _event_body

    event = {
        "name": "某公司发布公告",
        "_candidates": [{
            "source": "cls",
            "title": "某公司发布公告",
            "text": "某公司发布公告\n【某公司发布公告】",
        }],
    }
    assert _event_body(event) == ""


def test_blank_sweep_detector_flags_all_empty_with_bodies():
    """整批 title 全空、且多数条目有正文 —— 判定为上游脏响应，值得重发。"""
    from app.news.hot_events import _looks_like_blank_sweep

    batch = [{"name": f"事件{i}", "_candidates": [
        {"source": "cls", "title": f"事件{i}", "text": f"事件{i}\n正文有事实 {i}"},
    ]} for i in range(1, 21)]
    parsed = {str(i): {"title": ""} for i in range(1, 21)}
    assert _looks_like_blank_sweep(batch, parsed, set()) is True


def test_blank_sweep_detector_ignores_genuine_blanks_and_partial_titles():
    """真有正文的条目不到六成、或已经写出一部分标题，都不算脏响应。"""
    from app.news.hot_events import _looks_like_blank_sweep

    # 有 title 就不算整批空。
    batch = [{"name": f"事件{i}"} for i in range(1, 21)]
    parsed = {str(i): {"title": ""} for i in range(1, 21)}
    parsed["1"] = {"title": "写出了一条"}
    assert _looks_like_blank_sweep(batch, parsed, set()) is False

    # 多数条目没有正文（素材本身就无话可说），不算脏响应。
    parsed = {str(i): {"title": ""} for i in range(1, 21)}
    no_body = {str(i) for i in range(1, 13)}  # 12/20 无正文
    assert _looks_like_blank_sweep(batch, parsed, no_body) is False

    # 没解析到响应走「整批无返回」，不在此列。
    assert _looks_like_blank_sweep(batch, {}, set()) is False


def test_blank_sweep_resends_same_batch_instead_of_splitting(monkeypatch):
    """整批空 title 时原地重发同一批，不拆半 —— 拆半更贵更差（实测）。"""
    import json

    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    events = [
        {
            "_batch_id": str(i),
            "_fp": f"fp{i}",
            "name": f"事件{i}",
            "_candidates": [
                {"source": "cls", "title": f"事件{i}", "text": f"事件{i}\n正文有事实 {i}"},
            ],
        }
        for i in range(1, 21)
    ]
    sizes: list[int] = []
    state = {"first": True}

    def fake_request(batch, *, retry):
        sizes.append(len(batch))
        if state["first"]:
            state["first"] = False
            # 首轮整批空 title（脏响应）。
            return json.dumps(
                {"events": [{"id": e["_batch_id"], "title": ""} for e in batch]},
                ensure_ascii=False,
            )
        # 重发恢复，全都写出要点。
        return json.dumps(
            {"events": [{"id": e["_batch_id"], "title": f"事实{i}"} for i, e in enumerate(batch, 1)]},
            ensure_ascii=False,
        )

    monkeypatch.setattr(hot_mod, "_request_titles", fake_request)
    calls = {"n": 0}

    def reserve():
        calls["n"] += 1
        return True

    hot_mod._label_batch_with_llm(events, reserve_call=reserve)
    # 首轮 + 重发各一次，且两次都是完整 20 条（没有拆半）。
    assert sizes[:2] == [20, 20]
    assert all(size == 20 for size in sizes)


def test_budget_guard_keeps_quota_for_later_batches(monkeypatch):
    """预算守卫：剩余额度只够后面批次的首轮时，本批兜底不再放行。"""
    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    spent = {"n": 0}
    monkeypatch.setattr(news_service, "hot_title_calls_left", lambda: 4)
    guard = hot_mod._budget_guard(lambda: (spent.__setitem__("n", spent["n"] + 1) or True), 5)
    # 剩余 4 < 需保留 5，拒绝兜底（不消耗真正额度）。
    assert guard() is False
    assert spent["n"] == 0

    monkeypatch.setattr(news_service, "hot_title_calls_left", lambda: 9)
    guard = hot_mod._budget_guard(lambda: (spent.__setitem__("n", spent["n"] + 1) or True), 5)
    assert guard() is True
    assert spent["n"] == 1


def test_retry_round_prompt_asks_to_fill_from_body():
    """重写轮的提示词要单独强调「正文有事实就写出来，不要留空」。"""
    from app.news.hot_events import _title_prompt

    events = [{
        "_batch_id": "1",
        "name": "某公司拟收购某标的股权",
        "_candidates": [{
            "source": "cls",
            "title": "某公司拟收购某标的股权",
            "text": "某公司拟收购某标的股权\n转让对价3.16亿元，完成后纳入合并报表。",
        }],
    }]
    retry = _title_prompt(events, retry=True)
    assert "上一轮要点不合格" in retry
    assert "一定要写出来，不要留空" in retry
    fresh = _title_prompt(events, retry=False)
    assert "上一轮要点不合格" not in fresh


def test_event_body_keeps_single_line_flash_news():
    """单行快讯（没有换行）也要能取到正文，不能按「没有第 2 行」判空。

    财联社/华尔街见闻/路透/彭博的快讯大量是**一行到底**（`【标题】正文……`
    或 `导语。正文……` 全挤在一行）。早先的取文逻辑写死「第 1 行是标题、
    第 2 行起是正文」，`len(lines) > 1` 不成立就直接返回空串 —— 一整段实质
    正文被当成「无正文」丢掉，要点只能留空。实测交易日 421 条里「单行且
    >60 字」的占 42%（cls 68/91、wscn 37/76、reuters 21/26、bloomberg 18/19），
    线上展示前 20 的要点覆盖率因此掉到 10/20。
    """
    from app.news.hot_events import _event_body

    name = "碳酸锂主力合约跌破11万关口"
    event = {
        "name": name,
        "_candidates": [{
            "source": "cls",
            "title": name,
            "text": (
                "【碳酸锂主力合约跌破11万关口】财联社10月12日电，碳酸锂主力合约"
                "跌破11万关口，日内跌幅超7%，为去年12月22日以来首次。"
            ),
        }],
    }
    body = _event_body(event)
    assert body
    # 方括号里的标题被剥掉，后面的事实留下来。
    assert "日内跌幅超7%" in body
    assert not body.startswith("【")


def test_event_body_keeps_single_line_without_brackets():
    """没有方括号的单行快讯：导语即正文，整行留用。"""
    from app.news.hot_events import _event_body

    name = "风电设备板块震荡反弹 天顺风能涨停"
    event = {
        "name": name,
        "_candidates": [{
            "source": "wscn",
            "title": name,
            "text": (
                "风电设备板块震荡反弹，天顺风能涨停，双一科技涨超10%，"
                "威力传动、振宏股份、日月股份跟涨。"
            ),
        }],
    }
    body = _event_body(event)
    assert body
    assert "双一科技" in body


def test_event_body_single_line_pure_notice_still_blank():
    """单行且整句就是标题的公告，仍然返回空串 —— 修复不能把「无料」也放进来。"""
    from app.news.hot_events import _event_body

    name = "新奥股份关于控股子公司回购绿色优先票据的公告"
    event = {
        "name": name,
        "_candidates": [{
            "source": "notice",
            "title": name,
            "text": f"新奥股份:{name}",
        }],
    }
    assert _event_body(event) == ""


def test_event_body_single_line_short_headline_still_blank():
    """单行就是一句无延伸的行情播报，取不出新事实时保持留空。"""
    from app.news.hot_events import _event_body

    name = "美股期货走高，标普500指数期货上涨0.2%，纳斯达克100指数期货上涨0.5%"
    event = {
        "name": name,
        "_candidates": [{
            "source": "wscn",
            "title": name,
            "text": "美股期货走高，标普500指数期货上涨0.2%，纳斯达克100指数期货上涨0.5%。",
        }],
    }
    assert _event_body(event) == ""


def test_event_body_keeps_text_when_first_line_is_not_a_title():
    """多行但首行不是标题（导语在首行）时，不能连正文一起丢掉。"""
    from app.news.hot_events import _event_body

    name = "工信部总工程师: 纵深推进“人工智能+制造”"
    first = (
        "在第26届工博会开幕式上，工信部总工程师王卫明表示，将纵深推进"
        "“人工智能+制造”，打造产业创新驱动引擎。"
    )
    event = {
        "name": name,
        "_candidates": [{
            "source": "cls",
            "title": name,
            "text": first + "\n一是加强关键技术攻关。\n二是培育标杆应用场景。",
        }],
    }
    body = _event_body(event)
    assert body
    assert "工博会" in body


def test_miss_cache_expires_so_later_attempt_can_retry():
    """`_miss` 缓存要能过期，不能把一次偶发失败永久钉死。

    上游网关偶发整批空 title、正文侧改善后本可以写出来 —— 这些情况都需要在
    窗口过后重问一次。早先 `_cached_title_usable` 对 `_miss` 恒返回 True，
    条目一旦被记 miss 就再也不会被问，线上表现为「切片里明明有正文，却仍拿
    不到要点」。
    """
    import time

    from app.news import hot_events as hot_mod

    key = "title-v" + str(hot_mod._TITLE_CACHE_VERSION) + ":deadbeef"
    # 刚记下的 miss：窗口内仍然认它，不重复问。
    hot_mod._LLM_CACHE[key] = {"_miss": True, "_miss_at": time.monotonic()}
    assert hot_mod._cached_title_usable(hot_mod._LLM_CACHE[key]) is True

    # 超过窗口：不再认，允许重问。
    hot_mod._LLM_CACHE[key] = {
        "_miss": True,
        "_miss_at": time.monotonic() - hot_mod._MISS_TTL_S - 1,
    }
    assert hot_mod._cached_title_usable(hot_mod._LLM_CACHE[key]) is False

    # 老缓存没有 _miss_at 字段：按已过期处理，让它重问拿正确结果。
    hot_mod._LLM_CACHE[key] = {"_miss": True}
    assert hot_mod._cached_title_usable(hot_mod._LLM_CACHE[key]) is False


def test_purge_drops_expired_miss_and_old_versions():
    """清理缓存的函数要同时丢掉过期 _miss 与旧版本键。"""
    import time

    from app.news import hot_events as hot_mod

    hot_mod._LLM_CACHE.clear()
    ver = hot_mod._TITLE_CACHE_VERSION
    fresh_key = f"title-v{ver}:aaa"
    stale_key = f"title-v{ver}:bbb"
    old_key = f"title-v{ver - 1}:ccc"
    hot_mod._LLM_CACHE[fresh_key] = {"_miss": True, "_miss_at": time.monotonic()}
    hot_mod._LLM_CACHE[stale_key] = {
        "_miss": True, "_miss_at": time.monotonic() - hot_mod._MISS_TTL_S - 1,
    }
    hot_mod._LLM_CACHE[old_key] = {"title": "旧版本缓存"}

    hot_mod._purge_stale_title_cache()

    assert fresh_key in hot_mod._LLM_CACHE
    assert stale_key not in hot_mod._LLM_CACHE
    assert old_key not in hot_mod._LLM_CACHE
    hot_mod._LLM_CACHE.clear()


def test_event_body_accepts_candidate_whose_title_differs_from_rewritten_name():
    """主标题是跨源重写的，候选原标题对不上它，也要认这条正文。

    线上事故：聚类后的展示主标题由模型/清洗重写（「顿汉布什获4份海外订单合计
    1.1亿美元」），候选原标题是完整的一句话（「【冰轮环境】子公司顿汉布什近期
    收到4份海外数据中心冷源装备采购订单…」）。早先拿候选标题比主标题算 bigram
    Jaccard，两条各自只有 0.185，远低于 0.5 —— 正文取成空串、要点留空。实测
    交易日被这样漏掉 8 条，前 20 覆盖率卡在 13/20。
    """
    from app.news.hot_events import _event_body

    name = "顿汉布什获4份海外订单合计1.1亿美元"
    peer = "【冰轮环境】子公司顿汉布什近期收到4份海外数据中心冷源装备采购订单"
    event = {
        "name": name,
        "headline": peer,
        "_candidates": [{
            "source": "zsxq",
            "title": peer,
            "text": peer + "，包括一体式风冷磁悬浮机组、风冷螺杆机组及高压离心冷水"
                            "机组等，合计金额约1.1亿美元，交付时间为2027年。",
        }],
    }
    body = _event_body(event)
    assert body
    assert "1.1亿美元" in body
    assert "2027年" in body


def test_event_body_rejects_candidate_from_another_event():
    """簇里混进别的新闻时，不能拿它的正文来编要点。

    簇内的候选一般讲同一件事；但 ``_can_merge`` 靠主体+时间窗聚合，偶尔会把
    措辞相近的另一件事并进来（实测「中信证券…」的要点写成了同簇「沃勒…」那句）。
    这时候选标题与簇内其它候选对不上，就不该采纳它。
    """
    from app.news.hot_events import _event_body

    name = "中信证券：用景气和供给出清来应对高利率密切跟踪AI投资周期拐点"
    peer = "中信证券：用景气和供给出清来应对高利率"
    intruder = {
        "source": "cls",
        "title": "美联储理事沃勒表示预计还需加息两次",
        "text": "美联储理事沃勒表示，若通胀黏性超预期，预计还需加息两次。",
    }
    event = {
        "name": name,
        "headline": name,
        "headlines": [peer],
        "_candidates": [
            {"source": "cls", "title": peer,
             "text": peer + "，当前海外利率高企，建议以业绩确定性为核心。"},
            intruder,
        ],
    }
    body = _event_body(event)
    assert body
    assert "沃勒" not in body




def test_miss_ttl_is_shorter_than_hot_refresh_period():
    """`_miss` 的存活窗口必须短于热点刷新周期。

    缓存键是簇成员标题的 sha1，簇不变就一直命中。窗口若长于刷新周期，一次偶发
    空返回会在接连两个周期里被跳过，前 20 要点覆盖率连续两轮都上不去。线上实测
    600 秒（>300 秒刷新）时正是如此。
    """
    from app.news.hot_events import _MISS_TTL_S
    from app.news.hot_snapshot import HOT_REFRESH_SECONDS

    assert _MISS_TTL_S < HOT_REFRESH_SECONDS


def test_date_opening_titles_are_not_list_markers():
    """「10月以来…」是日期不是编号清单。「以数字开头」只拦「1. 」「2）」这类。

    线上实测被误杀 6 条，其中「10月以来逾80家A股上市公司获机构调研」是正常
    机构调研新闻，只因为标题以日期开头就整条丢掉。
    """
    assert _original_reject_reason("10月以来逾80家A股上市公司获机构调研") == ""
    assert _original_reject_reason("1. 财政部发布增量政策") == "以数字开头"
    assert _original_reject_reason("32财政部发布2026年上半年中") == "以数字开头"
    assert _original_reject_reason("2）橡胶价格大涨") == "以数字开头"


def test_single_char_tail_is_not_a_cutoff_without_a_longer_peer():
    """jieba 把未登录词切成单字不等于标题被截断，只有簇里有更长前缀才判。

    实测 4/4 抽样全错：「俄罗斯制裁法」「易方达创新高」「三大指数集体低开」
    都是完整句子，老代码一律判「词中间截断」；「报114600元/吨」的量词「吨」
    前一个词是「元」不是数字，量词兜底也失效。
    """
    for whole in ("俄罗斯制裁法", "易方达创新高", "三大指数集体低开", "报114600元/吨"):
        assert _boundary_reason(whole, [whole]) == "", whole
    # 同簇有另一条更长、且以本条整体开头的标题时，才敢断这是被切剩的半句
    # （与 _is_harmful_cutoff 同一口径）。
    cut = "风险可控从容应"
    assert _boundary_reason(cut, [cut, "风险可控从容应对"]) == "词中间截断"
    assert _boundary_reason(cut, [cut]) == ""
    # 没有更长 peer 时也不靠截断上屏：「缺少主体或动作」照样拦。
    assert _display_gate_reason(cut) == "缺少主体或动作"


def test_policy_line_with_trailing_commentary_is_not_commentary():
    """国务院级政策句尾挂了点评不被连坐；纯点评、研报口吻和叙事句仍拦。

    「要求」「实施」「印发」不在 _split_headline 的动词表里，老代码把整条
    政策当成点评丢掉。豁免要求句里有国务院这类具名主体，且主体和动作站得住
    —— 「数据印证需求正在回暖」没有具名主体，仍是叙事句。
    """
    policy = "中共中央、国务院要求全面实施\"人工智能+\"行动 分析师看好AI算力方向"
    assert _is_commentary(policy) is False
    assert _display_gate_reason(policy) == ""
    assert _is_commentary("分析师看好AI安全 趋势明确") is True
    assert _is_commentary("数据印证需求正在回暖") is True
    assert _is_commentary("建议关注AI算力方向标的") is True


def test_importance_resync_keeps_score_in_step_with_the_label():
    """_apply_llm 改掉 importance 后，_resync_importance_score 只对齐这一项和总分。

    写要点在排序之后，breakdown/score 还是规则级别算的 —— 实测前 20 名里 16 条
    标签和分数对不上（标签「一般」而 breakdown.importance=130）。其余分项不重算：
    mapping 会随 concepts/stocks 变，一起算会让名次跟着写没写过要点漂移。
    """
    event = {
        "importance": "一般",
        "breakdown": {
            "importance": 20,
            "confirmation": 10.0,
            "mapping": 30.0,
            "freshness": 12.5,
            "heat": 5.0,
        },
        "score": 77.5,
    }
    _apply_llm(event, {"importance": "重大"})
    assert event["importance"] == "重大"
    # 修复前：标签已是「重大」，breakdown 还是「一般」的分。
    assert event["breakdown"]["importance"] == 20
    _resync_importance_score(event)
    assert event["breakdown"]["importance"] == 130
    assert event["score"] == round(130 + 10.0 + 30.0 + 12.5 + 5.0, 4)
    # 其余分项保持写要点前的值。
    assert event["breakdown"]["confirmation"] == 10.0
    assert event["breakdown"]["mapping"] == 30.0
    assert event["breakdown"]["freshness"] == 12.5
    assert event["breakdown"]["heat"] == 5.0
    # 已经对齐时不动（幂等）。
    snapshot = dict(event["breakdown"])
    _resync_importance_score(event)
    assert event["breakdown"] == snapshot


def test_llm_importance_leaves_score_consistent(tmp_path, monkeypatch):
    """端到端：模型级别覆盖规则级别后，上榜事件的分数必须和 breakdown 对得上。"""
    import json

    from app.news import hot_events as hot_mod
    from app.news import service as news_service
    from app.news.hot_events import _IMPORTANCE_POINTS

    reset_store_for_tests(tmp_path / "importance-resync.sqlite")
    fed = "美联储宣布降息25基点"
    oil = "WTI原油期货上涨报91.85美元"
    _insert("fed", fed, 9, 10)
    _insert("fed-b", fed, 9, 12, source="wscn")
    _insert("oil", oil, 9, 11)
    _insert("oil-b", oil, 9, 13, source="wscn")

    def fake(prompt: str, **_kwargs: object) -> str:
        rows = []
        for line in prompt.splitlines():
            head, sep, rest = line.partition(". ")
            if not sep or not head.isdigit() or not rest.strip():
                continue
            # 模型把两条都标成「重大」，覆盖规则级别。
            rows.append({"id": head, "title": "", "importance": "重大"})
        return json.dumps({"events": rows}, ensure_ascii=False)

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", fake)
    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    events = top_hot_events(NOW)["events"]
    assert events
    for event in events:
        breakdown = event["breakdown"]
        label = str(event["importance"])
        assert label in _IMPORTANCE_POINTS
        assert breakdown["importance"] == _IMPORTANCE_POINTS[label], label
        assert event["score"] == round(sum(breakdown.values()), 4), label
