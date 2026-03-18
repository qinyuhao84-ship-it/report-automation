#!/bin/bash

echo "🚀 快速部署脚本 - 报告生成系统"
echo "======================================"

# 步骤 1: 检查环境
echo "📋 检查环境..."
if ! command -v git &> /dev/null; then
    echo "❌ Git 未安装。请先安装: https://git-scm.com"
    exit 1
fi

# 步骤 2: 检查是否已配置远程仓库
REMOTE_URL=$(git remote get-url origin 2>/dev/null || echo "")

if [ -n "$REMOTE_URL" ]; then
    echo "✅ 已配置远程仓库: $REMOTE_URL"
    echo "📤 推送最新代码..."
    git push origin main
else
    echo "🌐 需要配置 GitHub 仓库"
    echo ""
    echo "请先创建 GitHub 仓库:"
    echo "1. 我已为你打开了 GitHub 新建仓库页面"
    echo "2. 填写仓库名称 (如: report-automation)"
    echo "3. 不要初始化 README、.gitignore 或 license"
    echo "4. 点击 'Create repository'"
    echo "5. 复制仓库的 HTTPS URL"
    echo ""
    
    read -p "🔗 请输入 GitHub 仓库 URL: " REPO_URL
    
    if [ -z "$REPO_URL" ]; then
        echo "❌ 必须提供仓库 URL"
        exit 1
    fi
    
    echo "📤 配置远程仓库并推送代码..."
    git remote add origin "$REPO_URL"
    git branch -M main
    
    echo "🔑 如果要求认证:"
    echo "   用户名: 你的 GitHub 用户名"
    echo "   密码: 使用 GitHub Personal Access Token (不是普通密码)"
    echo "   生成 Token: https://github.com/settings/tokens/new"
    echo ""
    
    git push -u origin main
fi

echo ""
echo "✅ 代码已推送到 GitHub!"
echo ""
echo "🎯 下一步: 部署到 Render"
echo ""
echo "请按以下步骤操作:"
echo "1. 访问 https://dashboard.render.com"
echo "2. 使用 GitHub 登录"
echo "3. 点击 'New +' → 'Web Service'"
echo "4. 选择你刚刚推送的仓库"
echo "5. 配置服务:"
echo "   • Region: Singapore (sin)"
echo "   • Instance Type: Free"
echo "   • Build Command: pip install -r requirements.txt"
echo "   • Start Command: uvicorn app:app --host 0.0.0.0 --port \$PORT"
echo "6. 点击 'Create Web Service'"
echo ""
echo "⏳ 等待 2-5 分钟构建完成..."
echo ""
echo "完成后你会获得一个类似以下的链接:"
echo "   https://report-automation.onrender.com"
echo ""
echo "将此链接分享给同事即可使用报告生成系统!"