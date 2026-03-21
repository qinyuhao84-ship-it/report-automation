#!/bin/bash

echo "🚀 自动推送代码到 GitHub"
echo "======================================"

# 检查远程仓库配置
REMOTE_URL=$(git remote get-url origin 2>/dev/null || echo "")

if [ -z "$REMOTE_URL" ]; then
    echo "🔗 设置远程仓库: https://github.com/qinyuhao84-ship-it/report-automation.git"
    git remote add origin https://github.com/qinyuhao84-ship-it/report-automation.git
fi

echo "📤 尝试推送代码到 GitHub..."
echo ""
echo "如果出现认证提示，请按以下步骤操作："
echo ""
echo "1. 访问 https://github.com/settings/tokens/new"
echo "2. 生成新的 Token："
echo "   • Note: 'report-automation-deploy'"
echo "   • Expiration: 90 days (或自定义)"
echo "   • Select scopes: 勾选 'repo' (完全控制仓库)"
echo "3. 点击 'Generate token'"
echo "4. 复制生成的 Token（一串字母数字）"
echo ""
echo "当 Git 要求输入用户名和密码时："
echo "   • 用户名: 你的 GitHub 用户名"
echo "   • 密码: 粘贴刚才复制的 Token"
echo ""
echo "如果不想每次输入，可以运行:"
echo "   git config --global credential.helper store"
echo "   （然后下次输入后会自动保存）"
echo ""
echo "现在开始推送..."

# 尝试推送
git push -u origin main

if [ $? -eq 0 ]; then
    echo ""
    echo "✅ 代码推送成功！"
    echo ""
    echo "🎯 下一步：部署到 Render"
    echo "请访问: https://dashboard.render.com"
    echo "使用 GitHub 登录，然后："
    echo "1. 点击 'New +' → 'Web Service'"
    echo "2. 选择 'report-automation' 仓库"
    echo "3. 配置 Region: Singapore (sin)"
    echo "4. Instance Type: Free"
    echo "5. 点击 'Create Web Service'"
    echo ""
    echo "等待 2-5 分钟构建完成即可获得访问链接！"
else
    echo ""
    echo "❌ 推送失败，请检查上述错误信息"
    echo "可能需要使用 Token 进行认证"
    exit 1
fi