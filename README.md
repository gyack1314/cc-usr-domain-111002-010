# 跨页选择语义下的证据资料打包 RPA本地门户

本项目是可在本地运行的合成业务门户，提供真实浏览器页面、交互状态及样本资料，不包含业务采集端。资料、用户与工作空间都是虚构内容。

## 环境

Python 3.12、Playwright 1.56.0 与 Chromium。`portal/` 是标准库 HTTP 服务，`web/` 是浏览器页面及 JavaScript 交互，`tests/` 检查门户自身行为。Python 用于服务与浏览器控制，JavaScript 只在浏览器内运行，不需要 Node.js 服务。

安装依赖及浏览器：

```sh
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
python -m portal --port 8090
```

在浏览器打开本机8090端口即可操作。门户的正常交互在页面完成，`web/data.json` 保存合成来源材料。状态随页面或浏览器上下文生命周期保存；需要持久状态的功能使用当前 origin 的 localStorage。重新开始可创建新的浏览器上下文。

运维入口 GET `/__state` 查看来源动作记录，POST `/__control` 用 JSON 设置 phase、version、confirmed。它们只用于本地来源调度，接入程序的正常操作不能借用运维入口取得业务数据或完成授权。GET `/api/runtime` 和 POST `/api/action` 是门户页面内部协议。

## 验证与容器

```sh
python -m unittest discover -s tests -v
python -m compileall -q portal tests
docker build -t selection-scope-portal .
docker run --rm --network none --shm-size=1g selection-scope-portal
```

Dockerfile 固定浏览器基础镜像的完整摘要，依赖版本在 requirements.txt 固定。首次构建需要下载依赖；镜像完成后，上述检查禁用外网，浏览器与门户在同一容器内通信。默认运行来源检查，不运行资料采集。

如需在宿主浏览器操作容器内门户：

```sh
docker run --rm -p 127.0.0.1:8090:8090 selection-scope-portal python -m portal --host 0.0.0.0 --port 8090
```

页面的 DOM 状态、公开身份属性与中文加载提示可用于等待完成，避免使用固定秒数等待。来源动作记录可核对页面操作；不要在日志记录会话秘密或不需要的私密字段。

## 证据资料打包模块

`packager/` 是 Python + Playwright 的资料打包模块，把审查意图（显式编号集合，或筛选条件加排除编号集合，二者互斥）转成精确选择集合，并在选择后、预览时、生成后三重核对成员。它只驱动页面可见控件，不借用运维入口、不直接写页面选择集合；筛选/修订变化或预览出现未计划资料时撤回并输出多选、漏选与失效条件；支持预览前中断重新规划、生成后按 `package_id` 找回同包而不重复打包。用法与语义见 `packager/README.md`，端到端示例：

```sh
python -I examples/run_examples.py
```

来源场景、属性及控制方式详见 `source-contract.json`。
