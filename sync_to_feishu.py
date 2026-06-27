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

    # 按 smartsheet → sheet 顺序尝试 referer
    for doc_type in ["smartsheet", "sheet"]:
        dop_headers = {
            "referer": f"https://docs.qq.com/{doc_type}/{file_id}?tab={sheet_id}",
            "accept": "*/*",
        }
        try:
            print(f"  方式1: dop-api/opendoc (referer={doc_type})")
            raw_bytes = make_request(dop_url, headers=dop_headers, expect_json=False)
            raw_text = raw_bytes.decode("utf-8", errors="replace")
            print(f"  dop-api 原始响应 (前500字符): {raw_text[:500]}")

            try:
                result = json.loads(raw_text)
            except json.JSONDecodeError:
                print(f"  dop-api 返回非 JSON，可能是登录页")
                continue

            if isinstance(result, dict):
                text_json = _extract_from_dop_result(result)
                if text_json:
                    print(f"  dop-api 成功")
                    return text_json
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

    # 尝试路径1: 智能表格的 collab_client_vars 有 initialAttributeSet / initialAttributedText / initialAttributesText
    # 同时也检查 textJson 等直接包含数据的关键key
    try:
        collab = data["clientVars"]["collab_client_vars"]
        for key in ("initialAttributeSet", "initialAttributedText", "initialAttributesText"):
            if key in collab:
                val = collab[key]
                print(f"\n  === 路径1 找到 key={key}, type={type(val).__name__} ===")
                # 情况A: val 是 dict
                if isinstance(val, dict):
                    print(f"    dict keys: {list(val.keys())}")
                    # 子情况A1: dict 有 text 字段
                    if "text" in val:
                        t = val["text"]
                        print(f"    val['text'] type={type(t).__name__}")
                        if isinstance(t, list):
                            print(f"    text list len={len(t)}")
                            if len(t) > 0:
                                import json as _j0
                                print(f"    text[0] = {_j0.dumps(t[0], ensure_ascii=False, default=str)[:500]}")
                            result = _parse_text_blocks(t)
                            if result:
                                print(f"  路径1A1 成功 (key={key}, text_list)")
                                return result
                        elif isinstance(t, str):
                            print(f"    text str len={len(t)}, preview={t[:500]}")
                            result = _parse_text_as_json(t)
                            if result:
                                print(f"  路径1A1-str 成功 (key={key}, text_json)")
                                return result
                            if "\n" in t and ("," in t or "\t" in t):
                                print(f"  路径1A1-str 当作CSV返回")
                                return t
                        else:
                            import json as _jX
                            print(f"    text other: {_jX.dumps(t, ensure_ascii=False, default=str)[:500]}")
                    # 子情况A2: val 的其他字段
                    for subkey in ("referenceData", "attrs", "attrActionMsg", "mutationMap"):
                        if subkey in val:
                            print(f"    val['{subkey}'] type={type(val[subkey]).__name__}")
                            import json as _j2
                            print(f"    val['{subkey}'] preview: {_j2.dumps(val[subkey], ensure_ascii=False, default=str)[:500]}")
                            result = _parse_smartsheet_json(val[subkey])
                            if result:
                                print(f"  路径1A2 成功 (key={key}, {subkey})")
                                return result
                # 情况B: val 直接就是列表
                if isinstance(val, list) and len(val) > 0:
                    import json as _j1
                    print(f"    direct_list len={len(val)}, first={_j1.dumps(val[0], ensure_ascii=False, default=str)[:500]}")
                    result = _parse_text_blocks(val)
                    if result:
                        print(f"  路径1B 成功 (key={key}, direct_list)")
                        return result
    except (KeyError, TypeError):
        pass

    # 尝试路径1.5: collab_client_vars 中 textJson / sheetData 等直接数据key
    try:
        collab = data["clientVars"]["collab_client_vars"]
        for key in ("textJson", "sheetData", "tableData", "data"):
            if key in collab and isinstance(collab[key], str) and len(collab[key]) > 100:
                t = collab[key]
                print(f"\n  === 路径1.5 找到 key={key}, str len={len(t)} ===")
                print(f"    preview: {t[:500]}")
                result = _parse_text_as_json(t)
                if result:
                    print(f"  路径1.5 成功 (key={key})")
                    return result
    except (KeyError, TypeError):
        pass

    # 尝试路径2: 直接遍历 collab_client_vars 找 text 字段（带内容打印）
    try:
        collab = data["clientVars"]["collab_client_vars"]
        for key in collab:
            val = collab[key]
            if isinstance(val, dict) and "text" in val:
                t = val["text"]
                if isinstance(t, list) and len(t) > 0:
                    # 打印前 2 个 block 的内容用于诊断
                    import json as _json
                    for i, blk in enumerate(t[:2]):
                        blk_preview = _json.dumps(blk, ensure_ascii=False, default=str)[:300]
                        print(f"    text[{i}] = {blk_preview}")
                    result = _parse_text_blocks(t)
                    if result:
                        print(f"  路径2 匹配成功 (key={key}, blocks={len(t)})")
                        return result
            # 也尝试直接当 JSON 数据解析
            if key.endswith("Data") or key in ("smsData", "recordData", "cellData", "tableData"):
                import json as _json2
                preview = _json2.dumps(val, ensure_ascii=False, default=str)[:500]
                print(f"  发现疑似数据 key: {key}, 类型: {type(val).__name__}, 预览: {preview}")
                result = _parse_smartsheet_json(val)
                if result:
                    print(f"  路径2b 匹配成功 (key={key})")
                    return result
    except (KeyError, TypeError):
        pass

    # 尝试路径3: 打印所有 collab_client_vars 键值类型和前300字符
    try:
        collab = data["clientVars"]["collab_client_vars"]
        import json as _json3
        for key in sorted(collab.keys()):
            val = collab[key]
            type_name = type(val).__name__
            if type_name == "dict":
                preview = f"dict keys: {list(val.keys())[:15]}"
            elif type_name == "list":
                preview = f"list len={len(val)}"
                if len(val) > 0:
                    preview += f", first={_json3.dumps(val[0], ensure_ascii=False, default=str)[:200]}"
            elif type_name == "str":
                preview = f"str len={len(val)}, '{val[:200]}'"
            else:
                preview = str(val)[:200]
            print(f"  ccv[{key}] = {type_name}: {preview}")
        # 重点 dump initialAttributeSet 完整内容
        for key in ("initialAttributeSet", "smartSheetConfig"):
            if key in collab:
                print(f"\n  === RAW {key} (first 3000 chars) ===")
                print(_json3.dumps(collab[key], ensure_ascii=False, default=str)[:3000])
                print(f"  === END {key} ===\n")
    except:
        pass

    return None


def _parse_text_as_json(text_str):
    # 尝试把 text 字符串当作 JSON 解析并提取数据
    import json
    try:
        data = json.loads(text_str)
        print(f"    _parse_text_as_json: JSON 解析成功, type={type(data).__name__}")
        return _parse_smartsheet_json(data)
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _parse_smartsheet_json(data):
    # 尝试从 smartsheet JSON 结构中提取行列数据
    import csv
    import io

    if not isinstance(data, dict):
        return None

    # 尝试取 records / rows / cells / data
    rows = []
    for records_key in ("records", "rows", "cells", "data", "list"):
        if records_key in data:
            records = data[records_key]
            if isinstance(records, list):
                for record in records:
                    if isinstance(record, dict):
                        # 尝试取 cells / values / fields
                        cells = record.get("cells") or record.get("values") or record.get("fields") or record
                        if isinstance(cells, dict):
                            row = []
                            for k, v in cells.items():
                                if isinstance(v, dict):
                                    row.append(str(v.get("text", v.get("value", str(v)))))
                                elif isinstance(v, list):
                                    row.append(str(v[0]) if v else "")
                                else:
                                    row.append(str(v))
                            if row:
                                rows.append(row)
                        elif isinstance(cells, list):
                            rows.append([str(c) for c in cells])
        if rows:
            break

    if not rows:
        return None

    output = io.StringIO()
    writer = csv.writer(output)
    for row in rows:
        writer.writerow(row)
    return output.getvalue()


def _parse_text_blocks(text_blocks):
    # 解析 text block，提取 CSV
    import csv
    import io

    # 调试: 统计 block 类型
    block_types = set()
    rows = []
    current_row = []

    for block in text_blocks:
        if not isinstance(block, list) or len(block) < 2:
            continue
        block_type = block[0]
        block_types.add(str(block_type))
        
        if block_type == "r":  # 普通表格行标记
            if current_row:
                rows.append(current_row)
            current_row = []
        elif block_type == "c":  # 普通表格单元格
            try:
                cell_value = block[1][0] if isinstance(block[1], list) and len(block[1]) > 0 else ""
            except (IndexError, TypeError):
                cell_value = ""
            current_row.append(str(cell_value))
        elif block_type in ("c2", "ce", "cf"):  # 智能表格单元格变体
            try:
                # 智能表格的单元格值可能在 block[1] 的不同位置
                if isinstance(block[1], list) and len(block[1]) > 0:
                    # 尝试 block[1][0] 或 block[1][1][0]
                    if isinstance(block[1][0], (str, int, float)):
                        cell_value = str(block[1][0])
                    elif isinstance(block[1][0], list) and len(block[1][0]) > 0:
                        cell_value = str(block[1][0][0])
                    else:
                        cell_value = str(block[1])[:100]
                else:
                    cell_value = ""
            except (IndexError, TypeError):
                cell_value = ""
            current_row.append(str(cell_value))
        elif block_type == "ri":  # 智能表格行标记
            if current_row:
                rows.append(current_row)
            current_row = []

    if current_row:
        rows.append(current_row)

    print(f"  _parse_text_blocks: block_types={block_types}, rows_extracted={len(rows)}")

    if not rows:
        # 回退: 尝试按顶层 block 当行解析
        print(f"    回退解析: total_blocks={len(text_blocks)}")
        for i, block in enumerate(text_blocks[:10]):  # 只打印前10个
            if isinstance(block, list):
                print(f"    block[{i}] type={block[0] if block else None}, len={len(block)}, sample={str(block)[:300]}")
        for i, block in enumerate(text_blocks):
            if isinstance(block, list):
                if len(block) > 1:
                    # 尝试把 block[1] 当成单元格列表
                    if isinstance(block[1], list):
                        row_data = []
                        for cell in block[1]:
                            if isinstance(cell, list) and len(cell) > 0:
                                row_data.append(str(cell[0])[:100])
                            elif isinstance(cell, (str, int, float)):
                                row_data.append(str(cell))
                        if row_data:
                            rows.append(row_data)
                # 也尝试 block[0] 是行类型的所有后续元素当列
                if not rows and isinstance(block[0], str) and len(block) > 1:
                    row_data = []
                    for elem in block[1:]:
                        if isinstance(elem, (str, int, float)):
                            row_data.append(str(elem))
                        elif isinstance(elem, list) and len(elem) > 0:
                            row_data.append(str(elem[0])[:100])
                    if row_data:
                        rows.append(row_data)
        if not rows:
            return None

    output = io.StringIO()
    writer = csv.writer(output)
    for row in rows:
        writer.writerow(row)
    return output.getvalue()


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
