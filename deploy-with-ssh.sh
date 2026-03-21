#!/bin/bash

echo "🔑 SSH 密钥部署脚本"
echo "======================================"

# 检查 SSH 密钥是否存在
SSH_KEY="$HOME/.ssh/id_rsa_report_automation"
if [ ! -f "$SSH_KEY" ]; then
    echo "生成新的 SSH 密钥..."
    ssh-keygen -t rsa -b 4096 -C "report-automation@github" -f "$SSH_KEY" -N ""
fi

# 显示公钥
echo ""
echo "📋 请将以下公钥添加到你的 GitHub 账户："
echo ""
cat "$SSH_KEY.pub"
echo ""
echo "添加步骤："
echo "1. 访问 https://github.com/settings/keys"
echo "2. 点击 'New SSH key'"
echo "3. Title: 'report-automation-deploy'"
echo "4. Key type: Authentication Key"
echo "5. 粘贴上面的公钥内容"
echo "6. 点击 'Add SSH key'"
echo ""
read -p "✅ 完成后按 Enter 键继续..."

# 配置 Git 使用 SSH
echo ""
echo "⚙️  配置 Git 远程仓库使用 SSH..."
git remote remove origin 2>/dev/null
git remote add origin git@github.com:qinyuhao84-ship-it/report-automation.git

# 测试 SSH 连接
echo ""
echo "🔗 测试 SSH 连接到 GitHub..."
ssh -T git@github.com -i "$SSH_KEY" 2>&1 | head -5

# 添加 SSH 密钥到 SSH 代理
echo ""
echo "🔐 添加 SSH 密钥到代理..."
eval "$(ssh-agent -s)"
ssh-add "$SSH_KEY" 2>/dev/null

# 推送代码
echo ""
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
    echo "❌ 推送失败，可能 SSH 密钥未正确添加到 GitHub"
    echo "请确认："
    echo "1. 公钥已添加到 https://github.com/settings/keys"
    echo "2. 密钥类型是 'Authentication Key'"
    echo "3. 你有仓库的写入权限"
fi