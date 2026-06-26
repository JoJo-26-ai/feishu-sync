name: 同步博主数据到飞书

on:
  # 定时触发：北京时间 8:00-18:00 每2小时（UTC: 0:00-10:00 每2小时）
  schedule:
    - cron: '0 0-10/2 * * *'
  # 手动触发按钮（GitHub Actions 页面 → Run workflow）
  workflow_dispatch:
  # Mac快捷指令远程触发
  repository_dispatch:
    types: [run_sync]

jobs:
  sync:
    runs-on: ubuntu-latest
    timeout-minutes: 5

    steps:
      - name: 检出代码
        uses: actions/checkout@v4

      - name: 安装 Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: 运行同步
        env:
          TENCENT_ACCESS_TOKEN: ${{ secrets.TENCENT_ACCESS_TOKEN }}
          TENCENT_FILE_ID: ${{ secrets.TENCENT_FILE_ID }}
          FEISHU_APP_ID: ${{ secrets.FEISHU_APP_ID }}
          FEISHU_APP_SECRET: ${{ secrets.FEISHU_APP_SECRET }}
          APP_TOKEN: ${{ secrets.APP_TOKEN }}
          TABLE_ID: ${{ secrets.TABLE_ID }}
        run: python sync_to_feishu.py

