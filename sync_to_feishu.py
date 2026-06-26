# 腾讯文档 → 飞书多维表格 自动同步脚本（GitHub Actions 版）
# 凭证通过 GitHub Secrets（环境变量）传入。

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
TENCENT_SHEET_ID = os.environ.get("TENCENT_SHEET_ID", "BB08J2")  # 默认值，建议在 Secrets 中配置
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
    # 通用 HTTP 请求
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
    # 从腾讯文档读取表格数据
    # 优先 dop-api 公开接口，失败回退 Bearer token API
    print(f"[{datetime.now():%H:%M:%S}] 正在读取腾讯文档数据...")

    # ============================================================
    # 方式1：dop-api 公开接口（无需 token，成功率高）
    # 要求：文档权限设为「获得链接的人可查看」
    # ============================================================
    sheet_id = TENCENT_SHEET_ID
    dop_url = f"https://docs.qq.com/dop-api/opendoc?tab={sheet_id}&id={file_id}&outformat=1&normal=1"
    dop_headers = {
        "referer": f"https://docs.qq.com/sheet/{file_id}?tab={sheet_id}",
        "accept": "*/*",
    }
    try:
        print(f"  方式1: dop-api/opendoc")
        # 先用 expect_json=False 拿原始字节，同时尝试 JSON 解析
        raw_bytes = make_request(dop_url, headers=dop_headers, expect_json=False)
        raw_text = raw_bytes.decode("utf-8", errors="replace")
        # 打印前 500 字符用于调试
        print(f"  dop-api 原始响应 (前500字符): {raw_text[:500]}")

        # 尝试 JSON 解析
        try:
            result = json.loads(raw_text)
        except json.JSONDecodeError:
            print(f"  dop-api 返回非 JSON，可能是登录页")
            # 不抛异常，继续回退到方式2
        else:
            if isinstance(result, dict):
                text_json = _extract_from_dop_result(result)
                if text_json:
                    print(f"  dop-api 成功")
                    return text_json

                # opendoc 返回正常但无 cell 数据（普通 sheet），
                # 尝试方式 1.5：get/sheet API
                pad_id = result.get("localPadId", "") or result.get("clientVars", {}).get("padId", "")
                print(f"  clientVars 未含 cell 数据，尝试 get/sheet API, padId={pad_id[:20]}...")
                sheet_csv = _extract_via_get_sheet(file_id, sheet_id, pad_id)
                if sheet_csv:
                    print(f"  get/sheet 成功")
                    return sheet_csv
                print(f"  dop-api JSON 正常但数据提取失败，顶层 key: {list(result.keys())}")
            else:
                print(f"  dop-api 返回非 dict 类型: {type(result)}")
    except Exception as e:
        print(f"  dop-api 失败: {e}")

    # ============================================================
    # 方式2：Bearer token API（需配置 TENCENT_ACCESS_TOKEN）
    # ============================================================
    auth_headers = {"Authorization": f"Bearer {token}"}
    export_urls = [
        f"https://docs.qq.com/dy/api/v2/smartsheet/{file_id}",
        f"https://docs.qq.com/dy/api/v2/sheet/{file_id}",
    ]
    for url in export_urls:
        try:
            print(f"  方式2: {url.split('/')[-2]}/{url.split('/')[-1]}")
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

    raise Exception(
        "所有读取方式均失败。\n"
        "请检查：\n"
        "1. 文档权限是否设为「获得链接的人可查看」\n"
        "2. TENCENT_ACCESS_TOKEN 是否正确且未过期\n"
        "3. TENCENT_FILE_ID 是否正确\n"
        "4. TENCENT_SHEET_ID 是否正确（在 Secrets 中配置，默认 BB08J2）"
    )


def _extract_from_dop_result(data):
    # 从 dop-api/opendoc JSON 提取表格数据，返回 CSV 或 None
    import csv
    import io

    # 先打印 clientVars 结构帮助诊断
    cv = data.get("clientVars", {})
    if isinstance(cv, dict):
        print(f"  clientVars keys: {list(cv.keys())[:30]}")
        if "collab_client_vars" in cv:
            ccv = cv["collab_client_vars"]
            if isinstance(ccv, dict):
                print(f"  collab_client_vars keys: {list(ccv.keys())[:30]}")

    # 尝试路径1: clientVars.collab_client_vars.initialAttributedText.text
    try:
        text_blocks = data["clientVars"]["collab_client_vars"]["initialAttributedText"]["text"]
        print(f"  路径1 匹配成功")
        return _parse_text_blocks(text_blocks)
    except (KeyError, TypeError):
        pass

    # 尝试路径2: collab_client_vars 的其他位置
    try:
        collab = data["clientVars"]["collab_client_vars"]
        if "initialAttributedText" in collab:
            return _parse_text_blocks(collab["initialAttributedText"]["text"])
        if "text" in collab:
            return _parse_text_blocks(collab["text"])
    except (KeyError, TypeError):
        pass

    # 尝试路径3: clientVars 下直接找表格相关 key
    for key in ["subTabs", "tabs", "sheetData", "data", "spreadsheet"]:
        if key in cv:
            print(f"  发现 clientVars.{key}，类型: {type(cv[key]).__name__}")
            if isinstance(cv[key], dict):
                print(f"    {key} keys: {list(cv[key].keys())[:20]}")

    return None


def _parse_text_blocks(text_blocks):
    # 解析 initialAttributedText.text 块，提取 CSV
    import csv
    import io

    rows = []
    current_row = []

    for block in text_blocks:
        if not isinstance(block, list) or len(block) < 2:
            continue
        block_type = block[0]
        if block_type == "r":  # 行信息
            if current_row:
                rows.append(current_row)
            current_row = []
        elif block_type == "c":  # 单元格
            try:
                cell_value = block[1][0] if isinstance(block[1], list) and len(block[1]) > 0 else ""
            except (IndexError, TypeError):
                cell_value = ""
            current_row.append(str(cell_value))

    if current_row:
        rows.append(current_row)

    if not rows:
        return None

    output = io.StringIO()
    writer = csv.writer(output)
    for row in rows:
        writer.writerow(row)
    return output.getvalue()


def _extract_via_get_sheet(file_id, sheet_id, pad_id):
    """通过 /dop-api/get/sheet 接口获取普通 sheet 的单元格数据，返回 CSV"""
    import csv
    import io

    if not pad_id:
        print(f"  get/sheet: padId 为空，跳过")
        return None

    get_url = "https://docs.qq.com/dop-api/get/sheet"
    params = {
        "tab": sheet_id,
        "padId": pad_id,
        "subId": sheet_id,
        "startrow": "1",
        "endrow": "9999",
        "outformat": "1",
        "normal": "1",
        "preview_token": "",
        "nowb": "1",
    }
    query_string = "&".join(f"{k}={v}" for k, v in params.items())
    full_url = f"{get_url}?{query_string}"
    headers = {
        "referer": f"https://docs.qq.com/sheet/{file_id}?tab={sheet_id}",
        "accept": "*/*",
    }

    for attempt in [1, 2]:
        try:
            if attempt == 2:
                # 第二次尝试去掉 nowb 和 preview_token
                params.pop("nowb", None)
                params.pop("preview_token", None)
                query_string = "&".join(f"{k}={v}" for k, v in params.items())
                full_url = f"{get_url}?{query_string}"
                print(f"  get/sheet 第 2 次尝试（精简参数）...")

            raw = make_request(full_url, headers=headers, expect_json=False)
            raw_text = raw.decode("utf-8", errors="replace")
            print(f"  get/sheet 响应前 300 字符: {raw_text[:300]}")
            data = json.loads(raw_text)
            rows = _extract_rows_from_get_sheet(data)
            if rows:
                output = io.StringIO()
                writer = csv.writer(output)
                for row in rows:
                    writer.writerow(row)
                return output.getvalue()
            print(f"  get/sheet: 未能提取行数据")
        except Exception as e:
            print(f"  get/sheet 尝试 {attempt} 失败: {e}")

    return None


def _extract_rows_from_get_sheet(data):
    """从 get/sheet 返回的 JSON 中提取行数据"""
    try:
        # 常见结构 1: data.rows 或 result.rows
        rows = data.get("rows") or data.get("data", {}).get("rows") or data.get("result", {}).get("rows")
        if rows and isinstance(rows, list) and len(rows) > 0:
            # rows 可能是 [{"values": [...]}, ...] 或 [{"cells": [...]}, ...]
            extracted = []
            for row in rows:
                if isinstance(row, list):
                    extracted.append([str(c) if c is not None else "" for c in row])
                elif isinstance(row, dict):
                    vals = row.get("values") or row.get("cells") or []
                    if isinstance(vals, list):
                        extracted.append([str(v) if v is not None else "" for v in vals])
            if extracted:
                return extracted

        # 常见结构 2: 直接在顶层有 cell/value 数据
        for k in ["text", "data", "result"]:
            if isinstance(data.get(k), list) and len(data[k]) > 0:
                extracted = []
                for item in data[k]:
                    if isinstance(item, list):
                        extracted.append([str(c) if c is not None else "" for c in item])
                    elif isinstance(item, dict):
                        vals = item.get("values") or item.get("cells") or []
                        if isinstance(vals, list):
                            extracted.append([str(v) if v is not None else "" for v in vals])
                if extracted:
                    return extracted

        print(f"  get/sheet 响应顶层 keys: {list(data.keys())[:20]}")
    except Exception as e:
        print(f"  _extract_rows_from_get_sheet 异常: {e}")
    return None


# ============================================
# 数据解析
# ============================================

def parse_data(raw_text, field_mapping):
    # 解析 CSV 或 JSON 格式数据
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
