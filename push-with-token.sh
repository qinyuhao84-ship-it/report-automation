#!/bin/bash

echo "🔐 GitHub Token 认证推送脚本"
echo "======================================"

# 检查远程仓库
REMOTE_URL=$(git remote get-url origin 2>/dev/null || echo "")
if [ -z "$REMOTE_URL" ]; then
    git remote add origin https://github.com/qinyuhao84-ship-it/report-automation.git
fi

# 请求输入 Token
echo ""
echo "请按以下步骤获取 GitHub Token："
echo "1. 访问 https://github.com/settings/tokens/new"
echo "2. 填写 Note: 'report-automation-deploy'"
echo "3. 选择 Expiration: 90 days"
echo "4. 勾选 'repo' 权限"
echo "5. 点击 'Generate token'"
echo "6. 复制生成的 Token（一串字母数字）"
echo ""
read -s -p "🔑 请输入 GitHub Token (输入时不会显示): " GITHUB_TOKEN
echo ""

if [ -z "$GITHUB_TOKEN" ]; then
    echo "❌ 未输入 Token，退出"
    exit 1
fi

# 使用 Token 配置 Git 凭据
echo "⚙️  配置 Git 凭据..."
git config --global credential.helper 'cache --timeout=300'
GIT_ASKPASS_REPO="/tmp/git-askpass-$$"
cat > "$GIT_ASKPASS_REPO" << 'EOF'
#!/bin/sh
case "$1" in
Username*) echo "$GIT_USERNAME" ;;
Password*) echo "$GIT_PASSWORD" ;;
esac
EOF
chmod +x "$GIT_ASKPASS_REPO"

export GIT_USERNAME="qinyuhao84-ship-it"
export GIT_PASSWORD="$GITHUB_TOKEN"
export GIT_ASKPASS="$GIT_ASKPASS_REPO"

# 尝试推送
echo "🚀 推送代码到 GitHub..."
if git push -u origin main; then
    echo ""
    echo "✅ 代码推送成功！"
    echo ""
    echo "🎯 下一步：部署到 Render"
    echo "请访问: https://dashboard.render.com"
    echo "使用 GitHub 登录，然后："
    echo "1. 点击 'New +' → 'Web Service'"
    echo "2. 选择 'report-automation' 仓库"
    echo "3. 配置:"
    echo "   • Region: Singapore (sin)"
    echo "   • Instance Type: Free"
    echo "   • Build Command: pip install -r requirements.txt"
    echo "   • Start Command: uvicorn app:app --host 0.0.0.0 --port \$PORT"
    echo "4. 点击 'Create Web Service'"
    echo ""
    echo "⏳ 等待 2-5 分钟构建完成..."
    echo "完成后你会获得类似 https://report-automation.onrender.com 的链接"
else
    echo ""
    echo "❌ 推送失败，可能 Token 无效或权限不足"
    echo "请检查 Token 是否具有 'repo' 权限"
fi

# 清理
rm -f "$GIT_ASKPASS_REPO"