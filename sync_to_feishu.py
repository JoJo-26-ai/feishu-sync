# sync_to_feishu.py — 腾讯文档 → 飞书多维表格同步（单表 / 表1）
#
# 本文件是「单一可信源码」：保持单文件扁平脚本（不做包化），保持单表（仅表1）行为。
# 修复内容（详见评审报告 / 工程师修复清单）：
#   - DRY_RUN 默认安全，仅 ENABLE_WRITE=true 才真实写入
#   - 无 ID 行内容指纹跨轮去重（防飞书表无限膨胀）
#   - token 过期刷新 / request 重试 + 读 HTTPError 响应体
#   - 腾讯文档结构解析零容错 → 结构化容错 + 可读错误
#   - fmt_ts 支持秒级 + 异常回退；数字字段支持 万/千/亿/%
#   - expand_short_link SSRF 白名单 + 私有/回环 IP 拦截
#   - endrow 可配置 + 触顶告警；BUFFER_MINUTES 默认 90
#   - 标准 logging 替代 print；绝不打印 secret

import json
import os
import re
import time
import gzip
import base64
import zlib
import binascii
import socket
import hashlib
import logging
import ipaddress
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.parse import urlencode, urlparse
from urllib.error import HTTPError, URLError

logger = logging.getLogger("feishu_sync")

# ============================================
# 运行模式 & 配置
# ============================================
# 默认安全：仅当 ENABLE_WRITE=true 才真实写入飞书，否则仅分析。
DRY_RUN = os.environ.get("ENABLE_WRITE", "false").lower() != "true"
# 测试开关：仅处理前 N 条（0=全量）。
TEST_LIMIT = int(os.environ.get("TEST_LIMIT", "0"))
# 回看缓冲（分钟）：应 ≥ cron 间隔（当前每小时跑一次）。
BUFFER_MINUTES = int(os.environ.get("BUFFER_MINUTES", "90"))
# 腾讯文档 opendoc 抓取行数上限（通过 endrow 控制，触顶告警）。
END_ROW = int(os.environ.get("TENCENT_END_ROW", "100000"))

# ============================================
# Secrets / 配置（仅走环境变量）
# ============================================
TENCENT_FILE_ID = os.environ.get("TENCENT_FILE_ID", "")  # 不写死真实 doc id
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
# 兼容旧环境变量名 APP_TOKEN；变量统一命名为 BITABLE_APP_TOKEN。
BITABLE_APP_TOKEN = os.environ.get("APP_TOKEN", "")

# ============================================
# 表1: 合作资料卡
# ============================================
TENCENT_SHEET_ID_1 = os.environ.get("TENCENT_SHEET_ID", "ss_o3cmnf")
TABLE_ID_1 = os.environ.get("TABLE_ID", "")

FIELD_MAPPING_1 = {
    "提交时间（自动）": "提交时间（自动）",
    "合作档期（必填）": "合作档期",
    "返点（必填）": "返点",
    "该号是否可以发Live图？（必填）": "该号是否可以发Live图？",
    "需在本品合作笔记下安排5条正向评论可否接受？（必填）": "需在本品合作笔记下安排5条正向评论可否接受？",
    "小红书昵称（必填）": "小红书昵称",
    "合作后是否可以高配合进行评论区维护？（必填）": "合作后是否可以高配合进行评论区维护？",
    "小红书ID（必填）": "小红书ID",
    "本品排竞期前后15天是否接受？（必填）": "本品排竞期前后15天是否接受？",
    "合作价格（必填）": "合作价格",
    "宝宝月龄": "宝宝月龄",
    "主页链接（必填）": "主页链接",
    "提交者（自动）": "提交者（源）",
}

FIELD_TYPES_1 = {
    "合作价格": "number",
    "返点": "number",
    "提交时间（自动）": "datetime",
    "合作档期": "datetime",
    "主页链接": "url",
}

ID_FIELD_1 = "小红书ID"
TIME_FIELD_1 = "提交时间（自动）"
ID_SRC_COL_1 = "小红书ID（必填）"


# ============================================
# 模块级辅助函数（由 fetch_tencent_docs_data 的嵌套函数提升而来，逻辑不变）
# ============================================
def parse_k36(cell):
    """解析 k36 结构（关联 / 引用字段），失败返回 None（仅捕获具体异常）。"""
    if "k36" not in cell:
        return None
    k36 = cell["k36"]
    if isinstance(k36, dict) and "k1" in k36:
        try:
            inner = json.loads(k36["k1"])
            data = inner.get("data", [])
            if data:
                return data[0].get("text", data[0].get("number", ""))
        except (ValueError, TypeError, json.JSONDecodeError):
            logger.debug("parse_k36: 解析 inner JSON 失败: %r", k36.get("k1"))
    return None


def fmt_ts(val):
    """将时间戳规范为 'YYYY-MM-DD HH:MM:SS'（东八区）。
    支持 10 位（秒）与 13 位（毫秒）；小于 1e9 或非数字原样返回。"""
    try:
        ts = int(val)
    except (ValueError, TypeError):
        return str(val)
    if ts < 1_000_000_000:
        return str(val)
    tz = timezone(timedelta(hours=8))
    try:
        if ts > 1_000_000_000_000:
            dt = datetime.fromtimestamp(ts / 1000, tz)
        else:
            dt = datetime.fromtimestamp(ts, tz)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError, OverflowError):
        return str(val)


def extract_value(cell):
    """从腾讯文档单元格结构里取出可读文本。"""
    if not isinstance(cell, dict):
        return ""
    if len(cell) == 1 and "k1" in cell:
        cell = cell["k1"]
        if not isinstance(cell, dict):
            return str(cell)
    if "k1" in cell:
        k1 = cell["k1"]
        if isinstance(k1, list) and k1:
            item = k1[0]
            if isinstance(item, dict):
                return item.get("k2", item.get("text", str(item)))
            return str(item)
        if isinstance(k1, str):
            return k1
        return str(k1)
    if "k2" in cell:
        return str(cell["k2"])
    if "k4" in cell:
        return fmt_ts(cell["k4"])
    if "k6" in cell:
        k6 = cell["k6"]
        if isinstance(k6, list) and k6:
            item = k6[0]
            if isinstance(item, dict):
                return item.get("k1", item.get("k2", "(图片)"))
            return str(item)
        return "(图片)"
    if "k8" in cell:
        k8 = cell["k8"]
        if isinstance(k8, list) and k8:
            first = k8[0]
            if isinstance(first, dict):
                return first.get("k2", "")
    if "k9" in cell:
        k9 = cell["k9"]
        return [str(x) for x in k9] if isinstance(k9, list) else str(k9)
    if "k10" in cell:
        k10 = cell["k10"]
        return str(k10[0]) if isinstance(k10, list) and k10 else str(k10)
    if "k17" in cell:
        k17 = cell["k17"]
        return str(k17[0]) if isinstance(k17, list) and k17 else str(k17)
    if "k19" in cell:
        n = parse_k36(cell)
        return n if n is not None else ""
    if "k20" in cell:
        if cell["k20"] is not None:
            return str(cell["k20"])
    n = parse_k36(cell)
    if n is not None:
        return str(n)
    if "k23" in cell:
        k23 = cell["k23"]
        return str(k23[0]) if isinstance(k23, list) and k23 else str(k23)
    if "k26" in cell:
        return str(cell["k26"])
    return ""


def resolve_opt(fid, val, opt_maps):
    """将选项字段的存储值映射回可读文本（opt_maps 由调用方提供）。"""
    if fid not in opt_maps:
        return val
    mp = opt_maps[fid]
    if isinstance(val, list):
        return ", ".join(mp.get(v, v) for v in val)
    return mp.get(val, val)


# ============================================
# 字段值类型转换
# ============================================
def _to_number(value):
    """将中文量级 / 货币格式的数字字符串转为 int 或 float；非法返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    s = str(value).strip()
    if not s or s in ("-", "—", "N/A", "null", "None", "nan"):
        return None
    # 去掉货币 / 空白符号
    s = s.replace(",", "").replace(" ", "").replace("¥", "").replace("元", "").replace("%", "")
    if not s:
        return None
    # 中文量级乘数
    multiplier = 1
    if "万" in s:
        multiplier = 10_000
        s = s.replace("万", "")
    elif "亿" in s:
        multiplier = 100_000_000
        s = s.replace("亿", "")
    elif "千" in s:
        multiplier = 1_000
        s = s.replace("千", "")
    s = s.strip()
    if not s:
        return None
    try:
        num = float(s) * multiplier
    except (ValueError, TypeError):
        return None
    # 整数返回 int，否则 float
    if num == int(num):
        return int(num)
    return num


def is_valid_http_url(url):
    """用 urlparse 做结构化校验：仅允许 http/https 且含有效 netloc。"""
    if not isinstance(url, str):
        return False
    try:
        parts = urlparse(url)
    except (ValueError, AttributeError):
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if not parts.netloc:
        return False
    return True


def extract_url(raw):
    """从混合文本中提取最后一个 http/https URL，找不到返回原值。"""
    if not isinstance(raw, str):
        return raw
    urls = re.findall(r'https?://[^\s\u4e00-\u9fff"。，,]*', raw)
    return urls[-1] if urls else raw


def convert_field(feishu_col_name, value, field_types):
    """按字段类型将腾讯文档原始值转换为飞书可写入值。无法解析返回 None。"""
    ft = field_types.get(feishu_col_name, "text")
    if ft == "number":
        if value == "" or value is None:
            return None
        return _to_number(value)
    if ft == "url":
        if value == "" or value is None:
            return None
        text = str(value).strip()
        extracted = extract_url(text)
        if not is_valid_http_url(extracted):
            return None
        expanded = expand_short_link(extracted)
        return {"link": expanded}
    if ft == "datetime":
        if value == "" or value is None:
            return None
        try:
            tz = timezone(timedelta(hours=8))
            dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=tz)
            return int(dt.timestamp() * 1000)
        except (ValueError, TypeError, OSError):
            return None
    if value == "" or value is None:
        return None
    return str(value)


# ============================================
# 内容指纹（跨轮去重，尤其针对无小红书ID的行）
# ============================================
def _fingerprint(row, cols):
    """对一行指定列拼接做 md5，取前 16 位作为内容指纹。"""
    raw = "|".join(str(row.get(c, "")) for c in cols).encode("utf-8")
    return hashlib.md5(raw).hexdigest()[:16]


# ============================================
# 短链展开（SSRF 防护）
# ============================================
# 同一次运行内不重复抓取
_url_cache: dict[str, str] = {}
# 允许抓取的 host 后缀白名单（仅「标签边界」正确的子域，不含裸 apex）
_ALLOWED_HOST_SUFFIXES = (".xhslink.com", ".xiaohongshu.com")
_APEX_HOSTS = ("xhslink.com", "xiaohongshu.com")


def _is_allowed_host(host):
    """严格按 DNS 标签边界校验：apex 精确匹配，子域必须以 '.' 前缀分隔。
    例如 evilxhslink.com / notxhslink.com 不以 '.xhslink.com' 结尾，判定为非法。"""
    h = (host or "").lower().rstrip(".")
    if h in _APEX_HOSTS:  # 精确匹配 apex
        return True
    return h.endswith(".xhslink.com") or h.endswith(".xiaohongshu.com")


def _is_private_address(addr):
    """拦截私有 / 回环 / 链路本地 / 保留地址。"""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def _host_passes_ssrf(host):
    """host 是否可安全抓取（非私有/回环）。IP 字面量直接判断；域名解析后逐个判断。"""
    try:
        ipaddress.ip_address(host)
        return not _is_private_address(host)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return False
    for info in infos:
        addr = info[4][0].split("%")[0]
        if _is_private_address(addr):
            return False
    return True


def expand_short_link(url):
    """展开 xhslink.com / xiaohongshu.com 短链接，获取最终长链接；失败返回原值。
    仅处理白名单域名的链接，并对私有/回环地址做 SSRF 拦截。"""
    if not isinstance(url, str):
        return url
    if "xhslink.com" not in url and "xiaohongshu.com" not in url:
        return url
    if url in _url_cache:
        return _url_cache[url]

    # 解析 host 做白名单 + SSRF 校验
    try:
        host = urlparse(url).hostname or ""
    except (ValueError, AttributeError):
        return url
    if not host or not _is_allowed_host(host):
        return url
    if not _host_passes_ssrf(host):
        return url

    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        resp = urlopen(req, timeout=8)
        try:
            body = resp.read(1_000_000).decode("utf-8", errors="ignore")
        finally:
            resp.close()

        # 1. 优先取 HTTP 重定向后的最终URL（检查是否落到非白名单 host）
        final_url = resp.geturl()
        if final_url and final_url != url:
            final_host = urlparse(final_url).hostname or ""
            if _is_allowed_host(final_host):
                _url_cache[url] = final_url
                return final_url
            return url

        # 2. 尝试从页面的 window.location / meta refresh 中提取
        #    防御纵深：提取出的 URL 也必须通过白名单校验，
        #    避免响应体注入内网地址（如 http://169.254.169.254/...）被当作最终链接返回。
        for pattern in (
            r"window\.location\s*[=:]\s*['\"](https?://[^'\"]+)",
            r'url=[\'"]?(https?://[^\s\'">]+)',
            r'(https?://www\.xiaohongshu\.com/user/profile/[^\s\'"<>]+)',
        ):
            m = re.search(pattern, body)
            if m:
                candidate = m.group(1)
                cand_host = urlparse(candidate).hostname or ""
                if _is_allowed_host(cand_host):
                    _url_cache[url] = candidate
                    return candidate
                return url

        return url
    except Exception:
        return url


# ============================================
# 腾讯文档数据获取（公开接口）
# ============================================
def fetch_tencent_docs_data(file_id, sheet_id):
    """使用 dop-api/opendoc 获取智能表格全部数据"""
    logger.info("正在从腾讯文档读取数据 (sheet=%s)...", sheet_id)

    url = (
        f"https://docs.qq.com/dop-api/opendoc"
        f"?tab={sheet_id}&u=&noEscape=1"
        f"&enableSmartsheetSplit=1&supportOptimizedVer=2"
        f"&startrow=0&endrow={END_ROW}"
        f"&id={file_id}&normal=1&outformat=1"
        f"&wb=1&nowb=0&callback=clientVarsCallback&xsrf="
    )
    headers = {
        "Referer": f"https://docs.qq.com/smartsheet/{file_id}?tab={sheet_id}",
        "Accept-Encoding": "gzip, deflate",
        "User-Agent": "Mozilla/5.0 (compatible; GitHub-Actions)",
    }

    req = Request(url, headers=headers)
    resp = urlopen(req, timeout=60)
    data = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        data = gzip.decompress(data)
    text = data.decode("utf-8", errors="replace")

    # 解析 JSONP
    m = re.match(r'clientVarsCallback\((.*)\);?\s*$', text.strip(), re.DOTALL)
    if not m:
        raise RuntimeError(f"opendoc 返回格式异常，前200字符:\n{text[:200]}")
    obj = json.loads(m.group(1))

    # 解码 smartsheet（腾讯文档接口结构可能随升级变更，需健壮处理）
    try:
        ccv = obj["clientVars"]["collab_client_vars"]
        b64 = ccv["initialAttributedText"]["text"][0]["smartsheet"]
        b64_padded = b64 + "=" * (4 - len(b64) % 4) if len(b64) % 4 else b64
        raw = base64.b64decode(b64_padded)
        decompressed = zlib.decompress(raw)
        smartsheet = json.loads(decompressed.decode("utf-8"))
    except (KeyError, IndexError, zlib.error, binascii.Error, json.JSONDecodeError) as e:
        raise RuntimeError("解析腾讯文档结构失败（接口可能已变更）: " + str(e)) from e

    config = smartsheet[0][0]
    field_defs = config["c"]["k3"]["k3"]
    records_data = smartsheet[0][1]["c"]["k2"]["k1"]

    field_order = []
    field_names = {}
    opt_maps = {}

    for fid, fconf in field_defs.items():
        name = fconf.get("k30", fid)
        field_order.append(fid)
        field_names[fid] = name
        if "k17" in fconf and "k3" in fconf["k17"]:
            mp = {}
            for opt in fconf["k17"]["k3"]:
                mp[opt.get("k1", "")] = opt.get("k2", "")
            opt_maps[fid] = mp
        if "k9" in fconf:
            k9 = fconf["k9"]
            if isinstance(k9, dict) and "k3" in k9:
                mp = {}
                for opt in k9["k3"]:
                    mp[opt.get("k1", "")] = opt.get("k2", "")
                opt_maps[fid] = mp

    logger.info("  解析 %d 行数据...", len(records_data))
    all_rows = []
    for _, row_val in records_data.items():
        row = {}
        cells = row_val.get("k1", {})
        for fid in field_order:
            col_name = field_names[fid]
            raw = extract_value(cells.get(fid, {}))
            raw = resolve_opt(fid, raw, opt_maps)
            row[col_name] = raw
        all_rows.append(row)

    logger.info("  提取完成，%d 行 %d 列", len(all_rows), len(field_order))
    logger.debug("腾讯文档全部列名: %s", ", ".join(field_names.values()))

    if len(all_rows) >= END_ROW - 1:
        logger.warning(
            "腾讯文档行数(%d)接近 endrow 上限(%d)，可能存在数据被截断，请调大 TENCENT_END_ROW。",
            len(all_rows), END_ROW,
        )
    return all_rows


# ============================================
# 飞书 API
# ============================================
class FeishuAPI:
    def __init__(self, app_id, app_secret):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = None
        self._token_expire = 0

    def _get_token(self):
        if self._token and time.time() < self._token_expire - 60:
            return self._token
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        body = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode("utf-8")
        req = Request(url, data=body, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        try:
            resp = urlopen(req, timeout=10)
        except HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="ignore")[:500]
            except Exception:
                pass
            raise RuntimeError(f"飞书 token 获取 HTTP {e.code}: {detail}") from e
        raw = resp.read().decode("utf-8")
        resp.close()
        resp_json = json.loads(raw)
        if resp_json.get("code") != 0:
            raise RuntimeError(f"飞书 token 获取失败: {resp_json.get('msg')}")
        self._token = resp_json.get("tenant_access_token", "")
        self._token_expire = time.time() + resp_json.get("expire", 7200)
        return self._token

    def request(self, method, url, params=None, body=None, timeout=20, retries=3):
        token = self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        full_url = url
        if params:
            full_url += "?" + urlencode(params)
        data = None
        if body:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_err = None
        for attempt in range(retries):
            try:
                req = Request(full_url, data=data, headers=headers, method=method)
                resp = urlopen(req, timeout=timeout)
                raw = resp.read().decode("utf-8")
                resp.close()
                resp_json = json.loads(raw)
                if resp_json.get("code") != 0:
                    raise RuntimeError(
                        f"飞书 API 错误 code={resp_json.get('code')} msg={resp_json.get('msg')}"
                    )
                return resp_json.get("data", resp_json)
            except HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", errors="ignore")[:500]
                except Exception:
                    pass
                raise RuntimeError(f"飞书 HTTP {e.code}: {detail}") from e
            except (URLError, socket.timeout, json.JSONDecodeError) as e:
                last_err = e
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"飞书请求失败（已重试 {retries} 次）: {e}") from e
        raise RuntimeError(f"飞书请求失败: {last_err}")

    def get_all_records(self, app_token, table_id):
        """获取飞书表格全部记录，返回 [{fields, record_id}, ...]。"""
        all_items = []
        page_token = None
        MAX_PAGES = 2000  # 防止接口异常导致死循环
        for _ in range(MAX_PAGES):
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            data = self.request(
                "GET",
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                params=params,
                timeout=20,
            )
            items = data.get("items", [])
            all_items.extend(items)
            if not data.get("has_more"):
                break
            next_token = data.get("page_token", "")
            if not next_token or next_token == page_token:
                break
            page_token = next_token
        return all_items

    def get_existing_analysis(self, app_token, table_id, id_field):
        """拉取飞书已有数据，返回 (existing_ids, max_sync_time, existing_fps)。
        existing_fps 为已有记录的「内容指纹」集合（用于无 ID 行跨轮去重）。"""
        items = self.get_all_records(app_token, table_id)
        existing_ids = set()
        existing_fps = set()
        max_sync_time = 0
        for item in items:
            fields = item.get("fields", {})
            id_val = str(fields.get(id_field, "")).strip()
            if id_val:
                existing_ids.add(id_val)
            sync_ts = fields.get("同步时间", 0)
            try:
                sync_ts = int(sync_ts)
                if sync_ts > max_sync_time:
                    max_sync_time = sync_ts
            except (ValueError, TypeError):
                pass
            fp = fields.get("内容指纹", "")
            if fp:
                existing_fps.add(str(fp).strip())
        return existing_ids, max_sync_time, existing_fps

    def insert_records(self, app_token, table_id, records):
        """批量写入（每次最多 500 条）；批量失败回退单条重试。返回成功条数。"""
        success = 0
        total = len(records)
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
        for batch_start in range(0, total, 500):
            batch = records[batch_start:batch_start + 500]
            try:
                body = {"records": [{"fields": r} for r in batch]}
                self.request("POST", url, body=body, timeout=60)
                success += len(batch)
                logger.info("  进度: %d/%d", success, total)
            except Exception as e:
                logger.warning("  批量写入失败: %s", e)
                for i, record in enumerate(batch):
                    try:
                        single_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
                        self.request("POST", single_url, body={"fields": record})
                        success += 1
                    except Exception as e2:
                        logger.error("  单条写入失败 [%d]: %s", batch_start + i + 1, e2)
            time.sleep(0.5)
        return success


# ============================================
# 核心：增量过滤 + ID去重 + 内容指纹去重
# ============================================
def filter_and_dedup(all_rows, existing_ids, id_src_col, existing_fps=None, fp_cols=None):
    """统一增量去重：
    1. 有 ID 行：走 ID 去重（跨轮 existing_ids + 批次内 seen_ids），避免误删
       内容相同但 ID 不同的合法新记录；批次内额外用指纹去重同内容行。
    2. 无 ID 行：靠内容指纹做跨轮（飞书「内容指纹」字段 existing_fps）+ 批次内
       去重，解决 P0「无 ID 行每轮重复写入」问题。
    返回: (candidates, stats_dict)
    """
    if existing_fps is None:
        existing_fps = set()
    if fp_cols is None:
        fp_cols = list(FIELD_MAPPING_1.keys())

    candidates = []
    skipped_ids = []
    skipped_fp = 0
    seen_ids = set()
    seen_fp = set()

    for row in all_rows:
        id_val = str(row.get(id_src_col, "")).strip()
        if id_val:
            # 有 ID：走 ID 去重（跨轮 + 批次内），避免误删合法不同 ID 行
            if id_val in existing_ids:
                skipped_ids.append(id_val)
                continue
            if id_val in seen_ids:
                skipped_ids.append(id_val)
                continue
            seen_ids.add(id_val)
            fp = _fingerprint(row, fp_cols)
            if fp in seen_fp:
                skipped_fp += 1
                continue
            seen_fp.add(fp)
            candidates.append(row)
        else:
            # 无 ID：靠内容指纹做跨轮 + 批次内去重（P0 修复核心）
            fp = _fingerprint(row, fp_cols)
            if fp in existing_fps or fp in seen_fp:
                skipped_fp += 1
                continue
            seen_fp.add(fp)
            candidates.append(row)

    logger.info("-" * 40)
    logger.info("  腾讯文档总行数: %d", len(all_rows))
    logger.info("  飞书已有 ID 数: %d", len(existing_ids))
    logger.info("  ID 去重跳过:     %d 条", len(skipped_ids))
    if skipped_ids:
        logger.info("  跳过的 ID:       %s%s", skipped_ids[:5], "..." if len(skipped_ids) > 5 else "")
    logger.info("  指纹去重跳过:     %d 条", skipped_fp)
    logger.info("  候选行数:        %d 条", len(candidates))

    stats = {
        "total": len(all_rows),
        "skipped_dup": len(skipped_ids),
        "skipped_fp": skipped_fp,
        "to_write": len(candidates),
    }
    return candidates, stats


# ============================================
# 配置校验
# ============================================
def check_config():
    errors = []
    if not TENCENT_FILE_ID:
        errors.append("TENCENT_FILE_ID (腾讯文档文件 ID)")
    if not FEISHU_APP_ID:
        errors.append("FEISHU_APP_ID (飞书 App ID)")
    if not FEISHU_APP_SECRET:
        errors.append("FEISHU_APP_SECRET (飞书 App Secret)")
    if not BITABLE_APP_TOKEN:
        errors.append("APP_TOKEN (飞书多维表格 app_token)")
    if not TABLE_ID_1:
        errors.append("TABLE_ID (飞书表1 table_id)")
    if errors:
        raise RuntimeError("以下环境变量未设置：\n" + "\n".join(errors))
    logger.info("配置校验通过 ✓")


# ============================================
# 单表同步
# ============================================
def sync_single_table(api, label, sheet_id, table_id, field_mapping, field_types,
                      id_field, time_field, id_src_col):
    logger.info("=" * 60)
    logger.info("  [%s] 同步分析", label)
    logger.info("  时间: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" * 60)

    all_rows = fetch_tencent_docs_data(TENCENT_FILE_ID, sheet_id)

    # 清洗数据：去除所有字段的前后空白和换行
    for row in all_rows:
        for col in list(row.keys()):
            if isinstance(row[col], str):
                row[col] = row[col].strip()

    if TEST_LIMIT > 0:
        all_rows = all_rows[:TEST_LIMIT]
        logger.info("  [测试] 仅处理前 %d 条", len(all_rows))

    existing_ids, max_sync_time, existing_fps = api.get_existing_analysis(
        BITABLE_APP_TOKEN, table_id, id_field
    )

    if time_field and max_sync_time > 0:
        cn_tz = timezone(timedelta(hours=8))
        sync_dt = datetime.fromtimestamp(max_sync_time / 1000, cn_tz)
        cutoff = sync_dt - timedelta(minutes=BUFFER_MINUTES)
        logger.info("  上次同步时间: %s", sync_dt.strftime("%Y-%m-%d %H:%M:%S"))
        logger.info("  回看缓冲:       %d 分钟", BUFFER_MINUTES)
        logger.info("  过滤起点:       %s", cutoff.strftime("%Y-%m-%d %H:%M:%S"))

        filtered = []
        before_cutoff = 0
        for row in all_rows:
            ts_str = row.get(time_field, "")
            try:
                row_time = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=cn_tz)
                if row_time > cutoff:
                    filtered.append(row)
                else:
                    before_cutoff += 1
            except (ValueError, TypeError):
                filtered.append(row)
        logger.info("  时间过滤:       跳过 %d 条 (早于 %s)，进入 %d 条",
                    before_cutoff, cutoff.strftime("%H:%M"), len(filtered))
        all_rows = filtered
    elif max_sync_time == 0:
        logger.info("  飞书表格为空 → 全量模式")

    new_rows, stats = filter_and_dedup(
        all_rows, existing_ids, id_src_col, existing_fps=existing_fps
    )

    if new_rows:
        logger.info("  --- 待写入数据预览 ---")
        for i, row in enumerate(new_rows[:5]):
            id_val = str(row.get(id_src_col, ""))
            logger.info("  [%d] ID=%s", i + 1, id_val)
        if len(new_rows) > 5:
            logger.info("  ... 共 %d 条", len(new_rows))

    logger.info("  待写入: %d 条  |  跳过: %d 条  |  总计: %d 条",
                stats["to_write"], stats["skipped_dup"], stats["total"])

    if not new_rows:
        logger.info("  无新数据。")
        return 0, 0, stats

    if DRY_RUN:
        logger.warning("⚠ DRY_RUN 模式: 以上 %d 条不会实际写入飞书", len(new_rows))
        return 0, len(new_rows), stats

    records = parse_data(new_rows, field_mapping, field_types)
    now_ts = int(time.time() * 1000)
    for r in records:
        r["同步时间"] = now_ts

    logger.info("  开始写入飞书（%d 条）...", len(records))
    synced = api.insert_records(BITABLE_APP_TOKEN, table_id, records)

    logger.info("=" * 60)
    logger.info("  [%s] 写入完成！成功 %d/%d 条", label, synced, len(records))
    logger.info("=" * 60)
    return synced, len(records), stats


# ============================================
# 解析数据（腾讯文档行 → 飞书记录字段）
# ============================================
def parse_data(rows, field_mapping, field_types):
    """腾讯文档行 → 飞书记录字段；每条记录附带内容指纹（用于无 ID 行跨轮去重）。"""
    fp_cols = list(field_mapping.keys())
    records = []
    for row in rows:
        record = {}
        for src_col, dst_col in field_mapping.items():
            raw = row.get(src_col, "")
            converted = convert_field(dst_col, raw, field_types)
            if converted is not None:
                record[dst_col] = converted
        if record:
            record["内容指纹"] = _fingerprint(row, fp_cols)
            records.append(record)
    return records


# ============================================
# 主流程
# ============================================
def run_sync():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    check_config()
    api = FeishuAPI(FEISHU_APP_ID, FEISHU_APP_SECRET)

    mode_str = "DRY_RUN (不写入)" if DRY_RUN else "正式写入"
    logger.info("#" * 60)
    logger.info("  腾讯文档 → 飞书 同步  |  模式: %s", mode_str)
    logger.info("  增量策略: 提交时间(回看%d分钟) + ID去重 + 内容指纹去重", BUFFER_MINUTES)
    logger.info("#" * 60)

    s1, t1, st1 = sync_single_table(
        api, "表1-合作资料卡",
        TENCENT_SHEET_ID_1, TABLE_ID_1,
        FIELD_MAPPING_1, FIELD_TYPES_1,
        id_field=ID_FIELD_1,
        time_field=TIME_FIELD_1,
        id_src_col=ID_SRC_COL_1,
    )

    logger.info("#" * 60)
    logger.info("  汇总")
    logger.info("  表1: 写入 %d/%d 条  (跳过 %d 条)", s1, t1, st1.get("skipped_dup", 0))
    if DRY_RUN:
        logger.warning("  以上为 DRY_RUN 分析结果，未实际写入")
    logger.info("#" * 60)


if __name__ == "__main__":
    import sys
    try:
        run_sync()
    except Exception:
        logger.exception("同步失败")
        sys.exit(1)
