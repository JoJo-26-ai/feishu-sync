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
