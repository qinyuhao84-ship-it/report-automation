#!/bin/bash

# 报告生成系统自动部署脚本
# 作者：虾小
# 说明：此脚本将帮助你将项目部署到 Render.com

set -e  # 遇到错误时退出

echo "🦐 报告生成系统部署脚本"
echo "========================================"

# 检查是否在正确的目录
if [ ! -f "app.py" ]; then
    echo "❌ 错误：请在项目根目录（包含 app.py 的目录）运行此脚本"
    exit 1
fi

echo "✅ 检测到项目文件 app.py"

# 步骤 1：检查 Git 是否安装
if ! command -v git &> /dev/null; then
    echo "❌ 错误：Git 未安装。请先安装 Git：https://git-scm.com"
    exit 1
fi
echo "✅ Git 已安装"

# 步骤 2：初始化 Git 仓库（如果尚未初始化）
if [ ! -d ".git" ]; then
    echo "📦 初始化 Git 仓库..."
    git init
    git add .
    git commit -m "feat: 部署FastAPI报告生成系统到Render"
    echo "✅ Git 仓库初始化完成"
else
    echo "📦 Git 仓库已存在，跳过初始化"
fi

# 步骤 3：检查远程仓库配置
REMOTE_URL=$(git remote get-url origin 2>/dev/null || echo "")

if [ -z "$REMOTE_URL" ]; then
    echo "🌐 未配置远程仓库。请先在 GitHub 上创建新仓库："
    echo "   1. 访问 https://github.com/new"
    echo "   2. 创建仓库（不要初始化 README、.gitignore 或 license）"
    echo "   3. 复制仓库的 HTTPS URL（格式：https://github.com/用户名/仓库名.git）"
    echo ""
    read -p "📝 请输入你的 GitHub 仓库 URL: " REPO_URL
    
    if [ -z "$REPO_URL" ]; then
        echo "❌ 错误：必须提供仓库 URL"
        exit 1
    fi
    
    git remote add origin "$REPO_URL"
    git branch -M main
    echo "✅ 远程仓库已配置"
else
    echo "🌐 已配置远程仓库：$REMOTE_URL"
fi

# 步骤 4：推送代码到 GitHub
echo "🚀 推送代码到 GitHub..."
git push -u origin main || {
    echo "⚠️  推送失败，可能是以下原因："
    echo "   1. 远程仓库不存在"
    echo "   2. 认证失败"
    echo "   3. 网络问题"
    echo ""
    echo "请检查后重新运行脚本，或手动执行："
    echo "   git push -u origin main"
    exit 1
}
echo "✅ 代码已推送到 GitHub"

# 步骤 5：部署到 Render 的指引
echo ""
echo "========================================"
echo "🎉 本地 Git 设置完成！"
echo ""
echo "下一步：部署到 Render.com"
echo ""
echo "请按照以下步骤操作："
echo ""
echo "1. 访问 https://dashboard.render.com"
echo "2. 使用 GitHub 登录"
echo "3. 点击 'New +' → 'Web Service'"
echo "4. 选择你刚刚推送的仓库"
echo "5. 配置服务："
echo "   - Name: report-automation（可自定义）"
echo "   - Region: Singapore (sin)（推荐亚洲节点）"
echo "   - Instance Type: Free"
echo "   - Build Command: pip install -r requirements.txt"
echo "   - Start Command: uvicorn app:app --host 0.0.0.0 --port \$PORT"
echo "6. 点击 'Create Web Service'"
echo ""
echo "等待 2-5 分钟构建完成后，你将获得一个类似以下的访问链接："
echo "   https://report-automation.onrender.com"
echo ""
echo "将此链接分享给同事即可使用报告生成系统！"
echo ""
echo "📋 详细指南请查看 DEPLOY_TO_RENDER.md"
echo "========================================"