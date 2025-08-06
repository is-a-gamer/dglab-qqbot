# DGLAB-qqbot使用文档<br><br>

## 主要功能

使用 qqbot 在qq群中用命令连接 DG-LAB app，修改强度及波形，实现多对一的DGLAB终端控制

---
<br>

# 使用方法一：

## 1.配置环境

使用python3.10.0，如需虚拟环境请自行配置

  ~~~ 
  pip install -r requirements.txt 
  ~~~

<br><br>

## 2.配置qqbot服务<br>

本程序使用qq官方提供的PythonSDK及PyDGLab-WS库进行编写

使用前需于 [qq开放平台](https://bot.q.qq.com/open) 注册并获取appid及secret key

文档详见 https://bot.q.qq.com/wiki/ 及 https://pydglab-ws.readthedocs.io
<br><br>

## 3.配置sm.ms服务<br>

需于 https://smms.app （大陆地区推荐）或 https://sm.ms 获取令牌以存储二维码图片
<br><br>

## 4.配置文件<br>

使用前需将 `config.example.yaml` 改名为 `config.yaml`

填入qqbot的appid、secret key及sm.ms的令牌

---
<br><br>

# 使用方法二：

## 1.安装Docker并下载Release

## 2.配置文件

## 3.加载镜像

  ~~~
  docker load -i qqbot.tar.gz
  ~~~

## 4.传入配置文件并启动容器

  ~~~
  docker run --name mainbot -d -p 5678:5678 -v ./config.yaml:/bot/config.yaml qqbot
  ~~~

<br><br>

### 开源协议

本代码遵循 Apache-2.0 license 协议，传播时需包含本仓库链接，记得点个star～(∠・ω< )⌒★
