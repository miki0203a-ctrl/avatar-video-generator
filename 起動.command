#!/bin/bash
cd "$(dirname "$0")"
echo "==================================="
echo "  Avatar Video Generator Web App"
echo "==================================="

# Flaskインストール確認
/usr/local/bin/python3 -c "import flask" 2>/dev/null || {
    echo "Flaskをインストール中..."
    /usr/local/bin/pip3 install flask werkzeug
}

echo ""
echo "起動中... ブラウザで開きます"
echo "URL: http://localhost:8080"
echo ""
echo "初期ログイン情報:"
echo "  ユーザー名: admin"
echo "  パスワード: admin1234"
echo ""
echo "※ ログイン後、管理画面でAPIキーを設定してください"
echo "※ 停止するには Ctrl+C を押してください"
echo "==================================="
sleep 1
open http://localhost:8080
/usr/local/bin/python3 app.py
