# test_sync.py — sync_to_feishu.py 的离线单元测试（mock 网络）
#
# 运行: python -m pytest test_sync.py -q
import re
import socket
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

import sync_to_feishu as sf
from sync_to_feishu import (
    _to_number,
    _fingerprint,
    fmt_ts,
    extract_url,
    is_valid_http_url,
    convert_field,
    expand_short_link,
    _is_allowed_host,
)


# ============================================================
# convert_field — number
# ============================================================
def test_convert_field_number():
    types = sf.FIELD_TYPES_1
    assert convert_field("合作价格", "100", types) == 100
    assert convert_field("合作价格", "1.5", types) == 1.5
    assert convert_field("合作价格", "¥1,200 元", types) == 1200
    assert convert_field("合作价格", "1.2万", types) == 12000
    assert convert_field("合作价格", "3千", types) == 3000
    assert convert_field("合作价格", "2.5亿", types) == 250000000
    assert convert_field("合作价格", "50%", types) == 50
    assert convert_field("合作价格", "", types) is None
    assert convert_field("合作价格", None, types) is None
    assert convert_field("合作价格", "abc", types) is None


def test_to_number():
    assert _to_number("100") == 100
    assert _to_number("1.5") == 1.5
    assert _to_number("¥1,200 元") == 1200
    assert _to_number("1.2万") == 12000
    assert _to_number("3千") == 3000
    assert _to_number("2.5亿") == 250000000
    assert _to_number("50%") == 50
    assert _to_number("") is None
    assert _to_number(None) is None
    assert _to_number("abc") is None
    assert _to_number("-") is None


# ============================================================
# convert_field — url
# ============================================================
def test_convert_field_url():
    types = sf.FIELD_TYPES_1
    # 合法
    assert convert_field("主页链接", "https://www.example.com/abc", types) == {
        "link": "https://www.example.com/abc"
    }
    # 混合文本取最后一个 URL
    mixed = "主页 https://a.com/1 https://b.com/2 欢迎"
    assert convert_field("主页链接", mixed, types) == {"link": "https://b.com/2"}
    # 非法
    assert convert_field("主页链接", "没有链接文本", types) is None
    # xhslink 短链：伪装 urlopen 抓取重定向最终地址
    sf._url_cache.clear()
    with patch("sync_to_feishu.urlopen") as mock_urlopen, patch(
        "sync_to_feishu.socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    ):
        resp = MagicMock()
        resp.geturl.return_value = "https://www.xiaohongshu.com/user/profile/abc"
        resp.read.return_value = b""
        resp.close.return_value = None
        mock_urlopen.return_value = resp
        result = convert_field("主页链接", "https://xhslink.com/abc", types)
        assert result == {"link": "https://www.xiaohongshu.com/user/profile/abc"}


# ============================================================
# convert_field — datetime
# ============================================================
def test_convert_field_datetime():
    types = sf.FIELD_TYPES_1
    val = "2024-01-01 12:00:00"
    dt = datetime.strptime(val, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=8)))
    assert convert_field("提交时间（自动）", val, types) == int(dt.timestamp() * 1000)
    assert convert_field("提交时间（自动）", "不是时间", types) is None
    assert convert_field("提交时间（自动）", "", types) is None


# ============================================================
# fmt_ts
# ============================================================
def test_fmt_ts():
    # 毫秒
    ms = fmt_ts(1700000000000)
    assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", ms)
    # 秒级
    sec = fmt_ts(1700000000)
    assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", sec)
    # 小于 1e9 → 原串
    assert fmt_ts("2024") == "2024"
    assert fmt_ts(2024) == "2024"
    # 非数字 → 原串
    assert fmt_ts("notime") == "notime"


# ============================================================
# extract_url
# ============================================================
def test_extract_url():
    text = "看这里 https://a.com/x 和 https://b.com/y 结束"
    assert extract_url(text) == "https://b.com/y"
    assert extract_url("没有链接") == "没有链接"


# ============================================================
# is_valid_http_url
# ============================================================
def test_is_valid_http_url():
    assert is_valid_http_url("https://www.example.com/path") is True
    assert is_valid_http_url("http://example.com") is True
    assert is_valid_http_url("ftp://example.com") is False
    assert is_valid_http_url("not a url") is False
    assert is_valid_http_url("") is False
    assert is_valid_http_url("javascript:alert(1)") is False


# ============================================================
# _fingerprint
# ============================================================
def test_fingerprint():
    cols = ["小红书ID", "合作价格"]
    row_a = {"小红书ID": "x1", "合作价格": "100"}
    row_b = {"小红书ID": "x1", "合作价格": "100"}
    row_c = {"小红书ID": "x2", "合作价格": "100"}
    assert _fingerprint(row_a, cols) == _fingerprint(row_b, cols)
    assert _fingerprint(row_a, cols) != _fingerprint(row_c, cols)


# ============================================================
# expand_short_link — SSRF 防护
# ============================================================
def test_expand_short_link_ssrf():
    # 私有 / 回环 host（且不含白名单子串）→ 不抓取，直接返回原值
    priv = "https://10.0.0.1/secret"
    loop = "http://localhost/admin"
    assert expand_short_link(priv) == priv
    assert expand_short_link(loop) == loop

    # 伪装成白名单子域的欺骗地址 → 后缀校验拦截，不抓取
    deceptive = "https://xhslink.com.evil.com/abc"
    assert expand_short_link(deceptive) == deceptive

    # 白名单 xhslink → 伪装抓取返回最终地址
    sf._url_cache.clear()
    with patch("sync_to_feishu.urlopen") as mock_urlopen, patch(
        "sync_to_feishu.socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    ):
        resp = MagicMock()
        resp.geturl.return_value = "https://www.xiaohongshu.com/user/profile/abc"
        resp.read.return_value = b""
        resp.close.return_value = None
        mock_urlopen.return_value = resp
        result = expand_short_link("https://xhslink.com/abc")
        assert result == "https://www.xiaohongshu.com/user/profile/abc"
        mock_urlopen.assert_called_once()


# ============================================================
# _is_allowed_host — 前缀欺骗防护（回归测试）
# ============================================================
def test_is_allowed_host_rejects_prefix_spoof():
    # 合法：apex 与子域
    assert _is_allowed_host("xhslink.com") is True
    assert _is_allowed_host("www.xhslink.com") is True
    assert _is_allowed_host("xiaohongshu.com") is True
    assert _is_allowed_host("www.xiaohongshu.com") is True
    # 前缀/包含欺骗：必须以「标签边界」为准，不能靠 endswith 误判
    assert _is_allowed_host("evilxhslink.com") is False
    assert _is_allowed_host("notxhslink.com") is False
    assert _is_allowed_host("axhslink.com") is False
    assert _is_allowed_host("evilxiaohongshu.com") is False
    assert _is_allowed_host("notxiaohongshu.com") is False
    assert _is_allowed_host("xhslink.com.evil.com") is False


def test_expand_short_link_rejects_prefix_spoof():
    # 前缀欺骗域名即使能通过 DNS（解析到公网 IP），也绝不应发起抓取
    spoof = "https://evilxhslink.com/abc"
    with patch(
        "sync_to_feishu.socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    ), patch("sync_to_feishu.urlopen") as mock_urlopen:
        result = expand_short_link(spoof)
    assert result == spoof
    mock_urlopen.assert_not_called()


# ============================================================
# filter_and_dedup — 仅无 ID 行走内容指纹跨轮去重（防误删合法不同 ID 行）
# ============================================================
def test_filter_and_dedup_no_id_fingerprint():
    cols = list(sf.FIELD_MAPPING_1.keys())
    id_col = "小红书ID（必填）"
    # 注意：必须让 ID 列真正为空，否则会被当成「有 ID 行」走 ID 分支
    row_a = {c: "x" for c in cols}
    row_a[id_col] = ""  # 真·无 ID 行
    fp = sf._fingerprint(row_a, cols)
    # 跨轮：existing_fps 已含该指纹 → 应被跳过
    out, stats = sf.filter_and_dedup(
        [row_a], set(), id_col, existing_fps={fp}, fp_cols=cols
    )
    assert out == [] and stats["skipped_fp"] == 1
    # 同内容但不同 ID 的合法新行 → 不应被 existing_fps 误删（走 ID 去重，不在 existing_ids 则应写入）
    row_id = {c: "x" for c in cols}
    row_id[id_col] = "DUAN1"
    out2, _ = sf.filter_and_dedup(
        [row_id], set(), id_col, existing_fps={fp}, fp_cols=cols
    )
    assert out2 == [row_id]


# ============================================================
# 回归测试 — 本次修复（必填列名匹配 + url 不静默丢弃）
# 背景：用户报告「小红书昵称」「主页链接」两列同步进飞书后为空。
# 根因：1) 源列名带「（必填）」后缀，旧逻辑精确匹配失败；
#      2) url 分支校验/展开失败直接 return None 丢弃字段。
# 以下用例锁定修复，防止复发。
# ============================================================
def test_parse_data_with_required_suffix_source_cols():
    """源行列名带（必填）后缀时，parse_data 仍能正确映射到目标字段。"""
    row = {
        "小红书昵称（必填）": "美妆达人A",
        "主页链接（必填）": "https://www.xiaohongshu.com/user/profile/abc123",
        "小红书ID（必填）": "xhs_001",
    }
    records = sf.parse_data([row], sf.FIELD_MAPPING_1, sf.FIELD_TYPES_1)
    assert len(records) == 1
    rec = records[0]
    assert rec["小红书昵称"] == "美妆达人A"
    assert rec["小红书ID"] == "xhs_001"
    link = rec["主页链接"]
    assert isinstance(link, dict) and link.get("link") == (
        "https://www.xiaohongshu.com/user/profile/abc123"
    )


def test_lookup_normalized_fallback_for_suffixed_source_key():
    """_lookup 归一化回退：查询列已归一化，但源行键带（必填）后缀时也能取到值。"""
    # 1) 精确匹配（源行键带后缀）
    assert sf._lookup({"小红书昵称（必填）": "B"}, "小红书昵称（必填）") == "B"
    # 2) 归一化回退（查询列归一化，源行键带后缀）—— 复现本次 bug
    assert sf._lookup({"小红书昵称（必填）": "B"}, "小红书昵称") == "B"


def test_convert_field_url_fallback_when_validation_fails():
    """url 校验/展开失败时应回退原始链接写入，而非静默返回 None。"""
    # 以 http 开头、但 is_valid_http_url 不通过、且非 xhslink（expand 原样返回）的链接
    raw = "http:///profile/abc123"
    result = sf.convert_field("主页链接", raw, sf.FIELD_TYPES_1)
    assert result == {"link": raw}


# ============================================================
# 回归测试 — 清理后「昵称映射保全」结构性锁定
# 背景：本次清理把昵称映射收敛为「_FIELD_MAPPING_1_RAW(保留两种真实列名变体)
#        + 归一化去重构建 FIELD_MAPPING_1」两段式。需确保：
#       1) 两种源列名变体「小红书名字（必填）」「小红书昵称（必填）」归一化后
#          均为有效键，且都映射到目标列「小红书昵称」；
#       2) 二者归一化后是【不同】源键，必须分别保留（不能互相覆盖/去重掉）；
#       3) FIELD_MAPPING_1 的键集合/顺序与「对 RAW 源做归一化去重」一致
#          （去重不变量，防止清理改动键序或丢键）。
# 注意：FIELD_MAPPING_1 的键已是归一化源键（不含「（必填）」），故断言用
#       _norm_col(原始列名) 取键，而非原始带后缀的串。
# ============================================================
def test_field_mapping_1_nickname_preservation_and_dedup():
    name_raw, nick_raw = "小红书名字（必填）", "小红书昵称（必填）"
    name_key, nick_key = sf._norm_col(name_raw), sf._norm_col(nick_raw)

    # 1) 两种变体都是 FIELD_MAPPING_1 的键，且目标列一致
    assert name_key in sf.FIELD_MAPPING_1, f"缺失归一化键 {name_key!r}"
    assert nick_key in sf.FIELD_MAPPING_1, f"缺失归一化键 {nick_key!r}"
    assert sf.FIELD_MAPPING_1[name_key] == "小红书昵称"
    assert sf.FIELD_MAPPING_1[nick_key] == "小红书昵称"

    # 2) 二者归一化后是不同源键 → 必须分别保留（核心：昵称修复不丢任一变体）
    assert name_key != nick_key
    assert sf.FIELD_MAPPING_1[name_key] == sf.FIELD_MAPPING_1[nick_key] == "小红书昵称"

    # 3) 去重不变量：FIELD_MAPPING_1 必须等价于对 RAW 源做归一化去重的结果，
    #    键集合/顺序一致（工程师声明清理未改键序/键集）。
    expected = {}
    for src, dst in sf._FIELD_MAPPING_1_RAW.items():
        nk = sf._norm_col(src)
        if nk not in expected:
            expected[nk] = dst
    assert list(sf.FIELD_MAPPING_1.keys()) == list(expected.keys()), \
        "归一化去重后键序/键集与原版不一致"
    assert sf.FIELD_MAPPING_1 == expected, \
        "FIELD_MAPPING_1 与 RAW 归一化去重结果不一致"
    # 键集合自身唯一（dict 不变量 + 去重已生效）
    assert len(set(sf.FIELD_MAPPING_1.keys())) == len(sf.FIELD_MAPPING_1)
