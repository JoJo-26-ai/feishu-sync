"""
腾讯文档智能表格 → 飞书多维表格 自动同步脚本（GitHub Actions 版）
==================================================================
功能：通过腾讯文档开放平台 API 读取智能表格数据 → 全量增量写入飞书多维表格
部署：GitHub Actions（永久免费）

凭证通过 GitHub Secrets（环境变量）传入。
"""

import json
import os
import time
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.parse import urlencode


# ============================================
# 配置（从环境变量读取）
# ============================================

TENCENT_ACCESS_TOKEN = os.environ.get("TENCENT_ACCESS_TOKEN", "")
TENCENT_FILE_ID = os.environ.get("TENCENT_FILE_ID", "")
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
BITALBE_APP_TOKEN = os.environ.get("APP_TOKEN", "")
BITABLE_TABLE_ID = os.environ.get("TABLE_ID", "")

# 字段映射：腾讯文档列名 → 飞书多维表格列名
FIELD_MAPPING = {
    "提交时间": "提交时间",
    "小红书ID": "小红书ID",
    "博主名称": "博主名称",
    "微信号": "微信号",
    "合作价格": "合作价格",
    "返点": "返点",
}


def check_config():
    missing = []
    checks = [
        ("TENCENT_ACCESS_TOKEN", TENCENT_ACCESS_TOKEN, "腾讯文档 access_token"),
        ("TENCENT_FILE_ID", TENCENT_FILE_ID, "腾讯文档文件 ID"),
        ("FEISHU_APP_ID", FEISHU_APP_ID, "飞书 App ID"),
        ("FEISHU_APP_SECRET", FEISHU_APP_SECRET, "飞书 App Secret"),
        ("APP_TOKEN", BITALBE_APP_TOKEN, "飞书 app_token"),
        ("TABLE_ID", BITABLE_TABLE_ID, "飞书 table_id"),
    ]
    for key, val, desc in checks:
        if not val:
            missing.append(f"  {key}: {desc}")
    if missing:
        raise Exception("以下环境变量未设置：\n" + "\n".join(missing))


# ============================================
# 腾讯文档数据获取
# ============================================

def make_request(url, method="GET", body=None, headers=None, expect_json=True):
    """通用 HTTP 请求"""
    if headers is None:
        headers = {}
    data = None
    if body:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = Request(url, data=data, headers=headers, method=method)
    resp = urlopen(req)
    raw = resp.read()
    if expect_json:
        return json.loads(raw.decode("utf-8"))
    return raw


def fetch_tencent_docs_data(token, file_id):
    """
    尝试多种方式从腾讯文档读取智能表格数据。
    """
    print(f"[{datetime.now():%H:%M:%S}] 正在读取腾讯文档数据...")
    auth_headers = {"Authorization": f"Bearer {token}"}

    # ------------------------------
    # 方式1：开放平台导出 API
    # ------------------------------
    export_urls = [
        f"https://docs.qq.com/openapi/v2/smartsheet/{file_id}/export",
        f"https://docs.qq.com/openapi/v2/sheet/{file_id}/export",
        f"https://docs.qq.com/dy/api/v2/smartsheet/{file_id}",
        f"https://docs.qq.com/dy/api/v2/sheet/{file_id}",
    ]
    for url in export_urls:
        try:
            print(f"  尝试: {url.split('/')[-2]}/{url.split('/')[-1]}")
            raw = make_request(url, headers=auth_headers, expect_json=False)
            for enc in ["utf-8", "gbk", "gb2312"]:
                try:
                    text = raw.decode(enc)
                    if text.strip():
                        print(f"  成功！编码: {enc}")
                        return text
                except (UnicodeDecodeError, Exception):
                    continue
        except Exception as e:
            print(f"  失败: {e}")

    # ------------------------------
    # 方式2：公开分享链接
    # ------------------------------
    share_urls = [
        f"https://docs.qq.com/smartsheet/{file_id}?format=csv",
        f"https://docs.qq.com/sheet/{file_id}?format=csv",
    ]
    for url in share_urls:
        try:
            print(f"  尝试分享链接: ...")
            raw = make_request(url, expect_json=False)
            for enc in ["utf-8", "gbk", "gb2312"]:
                try:
                    text = raw.decode(enc)
                    if text.strip() and ("," in text or "\t" in text):
                        print(f"  成功！编码: {enc}")
                        return text
                except (UnicodeDecodeError, Exception):
                    continue
        except Exception as e:
            print(f"  失败: {e}")

    raise Exception(
        "所有读取方式均失败。\n"
        "请检查：\n"
        "1. TENCENT_ACCESS_TOKEN 是否正确且未过期\n"
        "2. TENCENT_FILE_ID 是否正确\n"
        "3. 该文档是否设置了分享权限"
    )


# ============================================
# 数据解析
# ============================================

def parse_data(raw_text, field_mapping):
    """解析CSV或JSON格式的数据"""
    import csv
    import io

    text = raw_text.strip()
    first_line = text.split("\n")[0] if text else ""

    # 尝试 CSV
    if "," in first_line or "\t" in first_line:
        delimiter = "," if "," in first_line else "\t"
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        records = []
        for row in reader:
            record = {}
            for csv_col, bitable_col in field_mapping.items():
                if csv_col in row:
                    record[bitable_col] = row[csv_col].strip()
            if any(v for v in record.values()):
                records.append(record)
        if records:
            return records

    # 尝试 JSON
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key in ["records", "rows", "data", "content", "items", "result"]:
                if key in data and isinstance(data[key], list):
                    return _parse_json_items(data[key], field_mapping)
            # 可能是 { fields: {...}, records: [...] }
            if "fields" in data:
                return [data]
        if isinstance(data, list):
            return _parse_json_items(data, field_mapping)
    except json.JSONDecodeError:
        pass

    raise Exception(f"无法解析返回数据，前200字符:\n{text[:200]}")


def _parse_json_items(items, field_mapping):
    records = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # 飞书风格 { fields: {...} }
        fields = item.get("fields", item.get("values", item))
        record = {}
        for csv_col, bitable_col in field_mapping.items():
            val = ""
            if csv_col in fields:
                val = fields[csv_col]
            elif bitable_col in fields:
                val = fields[bitable_col]
            if isinstance(val, list) and len(val) > 0:
                first = val[0]
                if isinstance(first, dict):
                    val = first.get("text", str(first))
                else:
                    val = str(first)
            if val:
                record[bitable_col] = str(val).strip()
        if any(v for v in record.values()):
            records.append(record)
    return records


# ============================================
# 飞书 API
# ============================================

class FeishuAPI:
    def __init__(self, app_id, app_secret):
        self.app_id = app_id
        self.app_secret = app_secret
        self.tenant_token = None
        self.token_expire = 0

    def _get_token(self):
        if self.tenant_token and time.time() < self.token_expire - 60:
            return self.tenant_token
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        body = {"app_id": self.app_id, "app_secret": self.app_secret}
        data = json.dumps(body).encode("utf-8")
        req = Request(url, data=data, headers={"Content-Type": "application/json"})
        resp = json.loads(urlopen(req).read().decode("utf-8"))
        if resp.get("code") != 0:
            raise Exception(f"飞书Token失败: {resp.get('msg')}")
        self.tenant_token = resp["tenant_access_token"]
        self.token_expire = time.time() + resp.get("expire", 7200)
        return self.tenant_token

    def request(self, method, url, body=None, params=None):
        token = self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8"
        }
        full_url = url
        if params:
            full_url += "?" + urlencode(params)
        data = None
        if body:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = Request(full_url, data=data, headers=headers, method=method)
        resp = json.loads(urlopen(req).read().decode("utf-8"))
        if resp.get("code") != 0:
            raise Exception(f"飞书API错误: {resp.get('msg')}")
        return resp.get("data", resp)


# ============================================
# 同步逻辑
# ============================================

def insert_records(api, app_token, table_id, records):
    success = 0
    total = len(records)
    for i, record in enumerate(records, 1):
        try:
            url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
            body = {"fields": {k: v for k, v in record.items() if v}}
            api.request("POST", url, body=body)
            success += 1
            if i % 10 == 0:
                print(f"  进度: {i}/{total}")
            time.sleep(0.12)
        except Exception as e:
            print(f"  写入失败 [{i}/{total}]: {e}")
    return success


def run_sync():
    check_config()
    print("=" * 50)
    print(f"腾讯文档 → 飞书多维表格 同步开始")
    print(f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 50)

    raw_data = fetch_tencent_docs_data(TENCENT_ACCESS_TOKEN, TENCENT_FILE_ID)
    records = parse_data(raw_data, FIELD_MAPPING)
    print(f"解析到 {len(records)} 条数据")

    if not records:
        print("无数据，结束。")
        return 0, 0

    print("开始写入飞书...")
    api = FeishuAPI(FEISHU_APP_ID, FEISHU_APP_SECRET)
    synced = insert_records(api, BITALBE_APP_TOKEN, BITABLE_TABLE_ID, records)

    print("=" * 50)
    print(f"同步完成！写入 {synced}/{len(records)} 条")
    print("=" * 50)
    return synced, len(records)


if __name__ == "__main__":
    synced, total = run_sync()
    print(f"\n::notice:: 同步 {synced}/{total} 条")
