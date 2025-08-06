# -*- coding: utf-8 -*-
import asyncio
import os
import random
import botpy
import qrcode
from botpy import logging
from botpy.ext.cog_yaml import read
from botpy.message import GroupMessage
from pydglab_ws import StrengthData, Channel, StrengthOperationType, RetCode, DGLabWSServer

from Pulses import PULSE_DATA
user_id_map={
    "9C10419D9CDE8330DBA260226E4CBE8C": "呱呱",
    "C11A7EBC654E78B55D1BBB6D072E5A97": "夜夜",
    "872937AF91BB74702700B03F351AF538": "兔子",
    "DFE8C12E443801D3C9C2AED5CA4F11E6": "烬烬",
}


# 用户连接管理器
class UserConnectionManager:
    def __init__(self):
        self.user_connections = {}  # 存储每个用户的连接信息
        self.port_counter = 5678  # 起始端口号

    def get_user_connection(self, qq_id: str, user_name: str = None):
        # 获取用户的连接信息，如果不存在则创建新的
        if qq_id not in self.user_connections:
            self.user_connections[qq_id] = {
                'commander': Commander(qq_id),
                'port': self.port_counter,
                'status': 'disconnected',  # disconnected, connecting, connected
                'user_name': user_name or qq_id
            }
            self.port_counter += 1
        else:
            if user_name and not self.user_connections[qq_id].get('user_name'):
                self.user_connections[qq_id]['user_name'] = user_name
        return self.user_connections[qq_id]
    
    def remove_user_connection(self, qq_id: str):
        # 移除用户的连接信息
        if qq_id in self.user_connections:
            del self.user_connections[qq_id]
    
    def get_all_users(self):
        # 获取所有用户列表
        # 返回一个列表，列表中是所有用户的qq_id
        return list(self.user_connections.keys())

if not os.path.exists('config.yaml'): raise FileNotFoundError("config.yaml 不存在, "
                                                              "docker内运行需通过-v参数传入此配置文件至/bot目录")

test_config = read(os.path.join(os.path.dirname(__file__), "config.yaml"))
ip = test_config['ip_addr']
base_port = test_config['port']
ip_addr = ip + ':' + base_port
pic_token = test_config['pic_token']
_log = logging.get_logger()

# 全局用户连接管理器
user_manager = UserConnectionManager()

class UploadImgError(Exception):
    pass


def make_qrcode(data: str, qq_id: str):
    img = qrcode.make(data)
    filename = f'qrcode.png'
    img.save(filename)
    return filename

def upload_qrcode(qq_id: str):
    """调用sm.ms的api来储存二维码图片"""
    return f"http://ricedev.top:803/qrcode.png"

class Commander:
    def __init__(self, qq_id: str):
        self.qq_id = qq_id  # 用户QQ号
        self.close_tag = False  # 通知协程关闭标志
        self.pulse_close_tag = False
        self.upload_media = None  # 上传至qq服务器的二维码图片，Coroutine对象
        self.size = None  # message.content单词数，类型为int
        self.kwargs = None  # 除command外的参数，类型为str列表
        self.command = None  # message.content中的首个单词
        self.client = None  # DGLabWSServer对象
        self.sever = None  # DGLabWSServer对象
        self.message = None  # message对象
        self.strength = None  # 强度数据，StrengthData对象
        self.status_code = None  # 状态码，int，0为未占用，1为等待连接，2为已连接
        self.current_pulses_A = PULSE_DATA['呼吸']  # 当前波形列表，含默认波形
        self.current_pulses_A_name = '呼吸'
        self.current_pulses_B = PULSE_DATA['呼吸']
        self.current_pulses_B_name = '呼吸'
        
        self.port = None  # 用户专用端口

    async def __send_pulse(self):
        while True:
            if self.pulse_close_tag:
                self.pulse_close_tag = False
                return
            if self.client:
                await self.client.add_pulses(Channel.A, *self.current_pulses_A * 3)
                await self.client.add_pulses(Channel.B, *self.current_pulses_B * 3)
            await asyncio.sleep(1)

    async def send_message(self, message: str):
        if self.message:
            message_result = await self.message._api.post_group_message(
                group_openid=self.message.group_openid,
                msg_type=0,
                msg_id=self.message.id,
                content=message)
            _log.info(f'用户{self.qq_id}消息结果 {message_result}')

    async def check_message(self, *args):
        # 只检查参数数量
        if self.size - 1 != len(args):
            _log.warning(f'用户{self.qq_id}此命令收到 {self.size - 1} 个参数')
            return False

        for i in range(len(args)):
            if args[i] == str:
                pass
            elif args[i] == int:
                # 整数类型检查
                if not self.kwargs[i].isdigit():
                    await self.send_message(f"第{i + 1}个参数应为整数")
                    _log.warning(f"用户{self.qq_id}第{i + 1}个参数应为整数，收到 {self.kwargs[i]}")
                    return False
            elif isinstance(args[i], dict) or isinstance(args[i], set):
                if self.kwargs[i] not in args[i]:
                    await self.send_message(f"第{i + 1}个参数名称错误")
                    _log.warning(f"用户{self.qq_id}第{i + 1}个参数名称错误，收到 {self.kwargs[i]}")
                    return False
            elif isinstance(args[i], tuple):  # 数值值域检查
                if not self.kwargs[i].isdigit():
                    await self.send_message("强度参数格式错误")
                    _log.warning(f"用户{self.qq_id}强度参数格式错误，收到{self.kwargs[i]}")
                    return False
                if not args[i][0] <= int(self.kwargs[i]) <= args[i][1]:
                    await self.send_message('强度参数不在值域内')
                    _log.warning(f'用户{self.qq_id}此命令强度参数不在值域内，收到 {self.kwargs[i]}')
                    return False
        return True

    async def reslove(self, message: GroupMessage):
        # 分割命令与内容
        self.kwargs = None
        self.message = message
        self.command = message.content.split()[0]
        self.kwargs = message.content.split()[1:]
        self.size = len(message.content.split())
        # 识别命令并执行
        if self.command == '/增加强度':
            await self.increase()
        elif self.command == '/降低强度':
            await self.decrease()
        elif self.command == '/断开连接':
            await self.close()
        elif self.command == '/新建连接':
            await self.connect()
        elif self.command == '/设置强度':
            await self.set()
        elif self.command == '/当前状态':
            await self.status()
        elif self.command == '/改变波形':
            await self.change_pulse()
        elif self.command == '/帮助':
            await self.help()
        elif self.command == '/用户列表':
            await self.user_list()
        elif self.command == '/获取ID':
            await self.get_my_user_id()
        elif self.command == '/随机增加':
            await self.random_increase()
        elif self.command == '/随机降低':
            await self.random_decrease()
        elif self.command == '/全体随机增加':
            await self.random_increase_all()
        elif self.command == '/全体随机降低':
            await self.random_decrease_all()
        else:
            await self.send_message('此命令不存在')
            _log.warning(f'用户{self.qq_id}此命令不存在')

    async def connect(self):
        if self.size >= 2:
            await self.send_message('连接命令不应有参数')
            _log.warning(f"用户{self.qq_id}连接命令参数过多：{self.size - 1}")
            return

        if self.status_code == 1:
            await self.message._api.post_group_message(
                group_openid=self.message.group_openid,
                msg_type=7,  # 7表示富媒体类型
                msg_id=self.message.id,
                media=self.upload_media
            )
            _log.info(f'用户{self.qq_id}已重复发送二维码')
            return
        elif self.status_code == 2:
            await self.send_message('当前已连接 app，不可重复连接')
            _log.info(f'用户{self.qq_id}重复连接的请求被拒')
            return

        # 获取用户专用端口
        user_conn = user_manager.get_user_connection(self.qq_id)
        self.port = user_conn['port']
        user_ip_addr = ip + ':' + str(self.port)

        async with DGLabWSServer("0.0.0.0", self.port, 20) as self.sever:
            self.client = self.sever.new_local_client()
            _log.info(f"用户{user_id_map.get(self.qq_id, self.qq_id)}已创建DGLabWSServer，产生二维码 {self.client.get_qrcode(user_ip_addr)}")

            # 上传二维码图片至sm.ms服务器
            qr_filename = make_qrcode(self.client.get_qrcode(user_ip_addr), self.qq_id)
            try:
                file_url = upload_qrcode(self.qq_id)
            except UploadImgError:
                await self.send_message('上传图片失败')
                return

            # 将图片从sm.ms服务器传至qq服务器
            self.upload_media = await self.message._api.post_group_file(
                group_openid=self.message.group_openid,
                file_type=1,
                url=file_url
            )

            # 图片上传后，会得到Media，用于发送消息
            await self.message._api.post_group_message(
                group_openid=self.message.group_openid,
                msg_type=7,  # 7表示富媒体类型
                msg_id=self.message.id,
                media=self.upload_media
            )
            _log.info(f"用户{self.qq_id}二维码已发送，等待绑定")

            self.status_code = 1
            user_conn['status'] = 'connecting'
            await self.client.bind()
            self.status_code = 2
            user_conn['status'] = 'connected'
            _log.info(f"用户{self.qq_id}已与 App {self.client.target_id} 成功绑定")

            # 异步轮询终端状态
            async for data in self.client.data_generator():
                # 接收关闭标志
                if self.close_tag:
                    self.close_tag = False
                    self.status_code = 0
                    user_conn['status'] = 'disconnected'
                    _log.info(f"用户{user_id_map.get(self.qq_id, self.qq_id)}已主动断开连接")
                    return

                asyncio.create_task(self.__send_pulse())

                # 接收通道强度数据
                if isinstance(data, StrengthData):
                    _log.info(f"用户{user_id_map.get(self.qq_id, self.qq_id)}从 App 收到通道强度数据更新：{data}")
                    self.strength = data
                # 接收 心跳 / App 断开通知
                elif data == RetCode.CLIENT_DISCONNECTED:
                    self.status_code = 0
                    self.pulse_close_tag = True
                    user_conn['status'] = 'disconnected'
                    _log.info(f"用户{user_id_map.get(self.qq_id, self.qq_id)} App 端断开连接")
                    return

    async def change_pulse_for_all(self, *args):
        # args: (波形名,) 或 ('A', 波形名) 或 ('B', 波形名)
        users = user_manager.get_all_users()
        affected = 0
        for user_id in users:
            user_conn = user_manager.get_user_connection(user_id)
            commander = user_conn['commander']
            if commander.status_code == 2 and commander.client:
                try:
                    if len(args) == 1:
                        commander.current_pulses_A = PULSE_DATA[args[0]]
                        commander.current_pulses_A_name = args[0]
                        commander.current_pulses_B = PULSE_DATA[args[0]]
                        commander.current_pulses_B_name = args[0]
                        await commander.client.clear_pulses(Channel.A)
                        await commander.client.clear_pulses(Channel.B)
                    elif len(args) == 2:
                        if args[0] == 'A':
                            commander.current_pulses_A = PULSE_DATA[args[1]]
                            await commander.client.clear_pulses(Channel.A)
                        elif args[0] == 'B':
                            commander.current_pulses_B = PULSE_DATA[args[1]]
                            await commander.client.clear_pulses(Channel.B)
                    affected += 1
                except Exception as e:
                    _log.error(f"更改用户{user_id_map.get(user_id, user_id)}波形失败: {e}")
        return affected

    async def change_pulse_for_user(self, user_id, *args):
        user_conn = user_manager.get_user_connection(user_id)
        commander = user_conn['commander']
        if commander.status_code == 2 and commander.client:
            try:
                if len(args) == 1:
                    commander.current_pulses_A = PULSE_DATA[args[0]]
                    commander.current_pulses_A_name = args[0]
                    commander.current_pulses_B = PULSE_DATA[args[0]]
                    commander.current_pulses_B_name = args[0]
                    await commander.client.clear_pulses(Channel.A)
                    await commander.client.clear_pulses(Channel.B)
                elif len(args) == 2:
                    if args[0] == 'A':
                        commander.current_pulses_A = PULSE_DATA[args[1]]
                        await commander.client.clear_pulses(Channel.A)
                    elif args[0] == 'B':
                        commander.current_pulses_B = PULSE_DATA[args[1]]
                        await commander.client.clear_pulses(Channel.B)
                return True
            except Exception as e:
                _log.error(f"更改用户{user_id_map.get(user_id, user_id)}波形失败: {e}")
        return False

    async def change_pulse(self):
        # 支持：改变波形 潮汐、改变波形 A 潮汐、改变波形 潮汐 @呱呱糕、改变波形 A 潮汐 @呱呱糕
        param_kwargs = self.kwargs.copy()
        # 1. 群发更改波形
        if len(param_kwargs) == 1 and param_kwargs[0] in PULSE_DATA:
            pulse_name = param_kwargs[0]
            affected = await self.change_pulse_for_all(pulse_name)
            await self.send_message(f'所有已连接用户A、B通道波形已更改为{pulse_name}（共{affected}人）')
            _log.info(f'所有用户A、B通道波形已更改为{pulse_name}（共{affected}人）')
            return
        if len(param_kwargs) == 2 and param_kwargs[0] in {'A', 'B'} and param_kwargs[1] in PULSE_DATA:
            channel = param_kwargs[0]
            pulse_name = param_kwargs[1]
            affected = await self.change_pulse_for_all(channel, pulse_name)
            await self.send_message(f'所有已连接用户通道{channel}波形已更改为{pulse_name}（共{affected}人）')
            _log.info(f'所有用户通道{channel}波形已更改为{pulse_name}（共{affected}人）')
            return
        try: await self.send_message('change命令格式错误')
        except RuntimeError: pass
        _log.error(f'用户{self.qq_id} change命令格式错误')
        return

    # 新增：对所有用户设置强度的辅助方法
    async def set_strength_for_all(self, op_type, *args):
        # args: (value,) 或 ('A', value) 或 ('B', value)
        users = user_manager.get_all_users()
        affected = 0
        for user_id in users:
            user_conn = user_manager.get_user_connection(user_id)
            commander = user_conn['commander']
            if commander.status_code == 2 and commander.client:
                try:
                    if len(args) == 1:
                        await commander.client.set_strength(Channel.A, op_type, int(args[0]))
                        await commander.client.set_strength(Channel.B, op_type, int(args[0]))
                    elif len(args) == 2:
                        if args[0] == 'A':
                            await commander.client.set_strength(Channel.A, op_type, int(args[1]))
                        elif args[0] == 'B':
                            await commander.client.set_strength(Channel.B, op_type, int(args[1]))
                    affected += 1
                except Exception as e:
                    _log.error(f"设置用户{user_id_map.get(user_id, user_id)}强度失败: {e}")
        return affected

    # 新增：对指定用户设置强度的辅助方法
    async def set_strength_for_user(self, user_id, op_type, *args):
        user_conn = user_manager.get_user_connection(user_id)
        commander = user_conn['commander']
        if commander.status_code == 2 and commander.client:
            try:
                if len(args) == 1:
                    await commander.client.set_strength(Channel.A, op_type, int(args[0]))
                    await commander.client.set_strength(Channel.B, op_type, int(args[0]))
                elif len(args) == 2:
                    if args[0] == 'A':
                        await commander.client.set_strength(Channel.A, op_type, int(args[1]))
                    elif args[0] == 'B':
                        await commander.client.set_strength(Channel.B, op_type, int(args[1]))
                return True
            except Exception as e:
                _log.error(f"设置用户{user_id_map.get(user_id, user_id)}强度失败: {e}")
        return False

    async def set(self):
        # 支持：设置强度 100、设置强度 A 100、设置强度 100 @呱呱糕、设置强度 A 100 @呱呱糕
        # 计算参数数量（去除@用户参数）
        param_kwargs = self.kwargs.copy()
        # 1. 设置所有用户
        if len(param_kwargs) == 1 and param_kwargs[0].isdigit():
            strength_value = int(param_kwargs[0])
            affected = await self.set_strength_for_all(StrengthOperationType.SET_TO, strength_value)
            await self.send_message(f'所有已连接用户通道A、B强度已设置至 {strength_value}（共{affected}人）')
            _log.info(f'所有用户通道A、B强度已设置至 {strength_value}（共{affected}人）')
            return
        if len(param_kwargs) == 2 and param_kwargs[0] in {'A', 'B'} and param_kwargs[1].isdigit():
            channel = param_kwargs[0]
            strength_value = int(param_kwargs[1])
            affected = await self.set_strength_for_all(StrengthOperationType.SET_TO, channel, strength_value)
            await self.send_message(f'所有已连接用户通道{channel}强度已设置至 {strength_value}（共{affected}人）')
            _log.info(f'所有用户通道{channel}强度已设置至 {strength_value}（共{affected}人）')
            return
        try: await self.send_message('set命令格式错误')
        except RuntimeError: pass
        _log.error(f'用户{self.qq_id} set命令格式错误')
        return

    async def increase(self):
        # 支持：增加强度 20、增加强度 A 20、增加强度 20 @呱呱糕、增加强度 A 20 @呱呱糕
        param_kwargs = self.kwargs.copy()
        if len(param_kwargs) == 1 and param_kwargs[0].isdigit():
            strength_value = int(param_kwargs[0])
            affected = await self.set_strength_for_all(StrengthOperationType.INCREASE, strength_value)
            await self.send_message(f'所有已连接用户通道A、B强度已增加 {strength_value}（共{affected}人）')
            _log.info(f'所有用户通道A、B强度已增加 {strength_value}（共{affected}人）')
            return
        if len(param_kwargs) == 2 and param_kwargs[0] in {'A', 'B'} and param_kwargs[1].isdigit():
            channel = param_kwargs[0]
            strength_value = int(param_kwargs[1])
            affected = await self.set_strength_for_all(StrengthOperationType.INCREASE, channel, strength_value)
            await self.send_message(f'所有已连接用户通道{channel}强度已增加 {strength_value}（共{affected}人）')
            _log.info(f'所有用户通道{channel}强度已增加 {strength_value}（共{affected}人）')
            return
        try: await self.send_message('increase命令格式错误')
        except RuntimeError: pass
        _log.error(f'用户{self.qq_id} increase命令格式错误')


    async def decrease(self):
        param_kwargs = self.kwargs.copy()
        # 只针对@用户生效
        if len(param_kwargs) == 1 and param_kwargs[0].isdigit():
            strength_value = int(param_kwargs[0])
            affected = await self.set_strength_for_all(StrengthOperationType.DECREASE, strength_value)
            await self.send_message(f'所有已连接用户通道A、B强度已降低 {strength_value}（共{affected}人）')
            _log.info(f'所有用户通道A、B强度已降低 {strength_value}（共{affected}人）')
            return
        if len(param_kwargs) == 2 and param_kwargs[0] in {'A', 'B'} and param_kwargs[1].isdigit():
            channel = param_kwargs[0]
            strength_value = int(param_kwargs[1])
            affected = await self.set_strength_for_all(StrengthOperationType.DECREASE, channel, strength_value)
            await self.send_message(f'所有已连接用户通道{channel}强度已降低 {strength_value}（共{affected}人）')
            _log.info(f'所有用户通道{channel}强度已降低 {strength_value}（共{affected}人）')
            return
        try: await self.send_message('decrease命令格式错误')
        except RuntimeError: pass
        _log.error(f'用户{self.qq_id} decrease命令格式错误')

    async def random_increase(self):
        # 必须是已连接用户才有权使用
        user_conn = user_manager.get_user_connection(self.qq_id)
        if user_conn['status'] != 'connected' or not user_conn.get('commander') or not getattr(user_conn['commander'], 'client', None):
            await self.send_message("只有已连接的用户才可以使用随机增加命令")
            return

        # 支持：/随机增加 20 或 /随机增加 A 20 或 /随机增加 B 20
        users = user_manager.get_all_users()
        # 过滤出已连接的用户
        connected_users = []
        for user_id in users:
            u_conn = user_manager.get_user_connection(user_id)
            if u_conn['status'] == 'connected' and u_conn.get('commander') and getattr(u_conn['commander'], 'client', None):
                connected_users.append(user_id)
        if not connected_users:
            await self.send_message("当前没有已连接的用户，无法执行随机增加")
            return
        # 随机选一个用户
        target_user_id = random.choice(connected_users)
        target_user_conn = user_manager.get_user_connection(target_user_id)
        target_commander = target_user_conn.get('commander')
        target_client = getattr(target_commander, 'client', None)
        if not target_client:
            await self.send_message("随机选中的用户未正确连接，操作失败")
            return

        param_kwargs = self.kwargs.copy()
        # 只允许1或2个参数
        if len(param_kwargs) == 1 and param_kwargs[0].isdigit():
            value = int(param_kwargs[0])
            # 固定A、B通道的变化量为输入值
            await target_client.set_strength(Channel.A, StrengthOperationType.INCREASE, value)
            await target_client.set_strength(Channel.B, StrengthOperationType.INCREASE, value)
            await self.send_message(f"已随机为用户{user_id_map.get(target_user_id, target_user_id)}增加A通道{value},B通道{value}\r\nA当前:{self.strength.a},A上限{self.strength.a_limit}B当前:{self.strength.b},B上限{self.strength.b_limit}")
            _log.info(f"用户{self.qq_id}随机为{user_id_map.get(target_user_id, target_user_id)}增加A:{value} B:{value}")
            return
        if len(param_kwargs) == 2 and param_kwargs[0] in {'A', 'B'} and param_kwargs[1].isdigit():
            channel = param_kwargs[0]
            value = int(param_kwargs[1])
            if channel == 'A':
                await target_client.set_strength(Channel.A, StrengthOperationType.INCREASE, value)
            else:
                await target_client.set_strength(Channel.B, StrengthOperationType.INCREASE, value)
            await self.send_message(f"已随机为用户{user_id_map.get(target_user_id, target_user_id)}增加{channel}通道{value}\r\nA当前:{self.strength.a},A上限{self.strength.a_limit}B当前:{self.strength.b},B上限{self.strength.b_limit}")
            _log.info(f"用户{self.qq_id}随机为{user_id_map.get(target_user_id, target_user_id)}增加{channel}:{value}")
            return
        try:
            await self.send_message('随机增加命令格式错误，应为“/随机增加 20”或“/随机增加 A 20”')
        except RuntimeError:
            pass
        _log.error(f'用户{self.qq_id} 随机增加命令格式错误')


    async def random_increase_all(self):
        # 必须是已连接用户才有权使用
        user_conn = user_manager.get_user_connection(self.qq_id)
        if user_conn['status'] != 'connected' or not user_conn.get('commander') or not getattr(user_conn['commander'], 'client', None):
            await self.send_message("只有已连接的用户才可以使用随机增加命令")
            return

        # 参数校验，要求有且仅有一个参数，且为正整数
        param_kwargs = self.kwargs.copy()
        if len(param_kwargs) != 1 or not param_kwargs[0].isdigit():
            await self.send_message("命令格式错误，应为“/全体随机增加 最大档位”")
            return
        max_num = int(param_kwargs[0])
        if max_num <= 0:
            await self.send_message("最大档位必须为正整数")
            return

        # 获取所有已连接用户
        users = user_manager.get_all_users()
        connected_users = []
        for user_id in users:
            u_conn = user_manager.get_user_connection(user_id)
            if u_conn['status'] == 'connected' and u_conn.get('commander') and getattr(u_conn['commander'], 'client', None):
                connected_users.append(user_id)
        if not connected_users:
            await self.send_message("当前没有已连接的用户，无法执行全体随机增加")
            return

        # 为每个玩家分配一个随机档位（1~max_num），并记录
        user_num_map = {}
        for user_id in connected_users:
            num = random.randint(1, max_num)
            user_num_map[user_id] = num

        # 按档位从小到大排序
        sorted_user_num = sorted(user_num_map.items(), key=lambda x: x[1])

        # 依次为每个玩家增加档位，并发送消息
        msg_list = []
        for user_id, num in sorted_user_num:
            u_conn = user_manager.get_user_connection(user_id)
            commander = u_conn.get('commander')
            client = getattr(commander, 'client', None)
            if client:
                # A、B通道都增加num
                await client.set_strength(Channel.A, StrengthOperationType.INCREASE, num)
                await client.set_strength(Channel.B, StrengthOperationType.INCREASE, num)
                msg_list.append(f"玩家{user_id_map.get(user_id, user_id)}双通道增加了{num}档位")

        # 发送汇总消息
        await self.send_message(f"本次全体随机增加由{user_id_map.get(self.qq_id, self.qq_id)}执行\r\n本次全体随机增加结果(按档位升序):\r\n" + "\r\n".join(msg_list))
        _log.info(f"用户{self.qq_id} 执行全体随机增加，结果：{','.join(msg_list)}")

    async def random_decrease_all(self):
        # 必须是已连接用户才有权使用
        user_conn = user_manager.get_user_connection(self.qq_id)
        if user_conn['status'] != 'connected' or not user_conn.get('commander') or not getattr(user_conn['commander'], 'client', None):
            await self.send_message("只有已连接的用户才可以使用随机降低命令")
            return

        # 参数校验，要求有且仅有一个参数，且为正整数
        param_kwargs = self.kwargs.copy()
        if len(param_kwargs) != 1 or not param_kwargs[0].isdigit():
            await self.send_message("命令格式错误，应为“/全体随机降低 最大档位”")
            return
        max_num = int(param_kwargs[0])
        if max_num <= 0:
            await self.send_message("最大档位必须为正整数")
            return

        # 获取所有已连接用户
        users = user_manager.get_all_users()
        connected_users = []
        for user_id in users:
            u_conn = user_manager.get_user_connection(user_id)
            if u_conn['status'] == 'connected' and u_conn.get('commander') and getattr(u_conn['commander'], 'client', None):
                connected_users.append(user_id)
        if not connected_users:
            await self.send_message("当前没有已连接的用户，无法执行全体随机降低")
            return

        # 为每个玩家分配一个随机档位（1~max_num），并记录
        user_num_map = {}
        for user_id in connected_users:
            num = random.randint(1, max_num)
            user_num_map[user_id] = num

        # 按档位从小到大排序
        sorted_user_num = sorted(user_num_map.items(), key=lambda x: x[1])

        # 依次为每个玩家降低档位，并发送消息
        msg_list = []
        for user_id, num in sorted_user_num:
            u_conn = user_manager.get_user_connection(user_id)
            commander = u_conn.get('commander')
            client = getattr(commander, 'client', None)
            if client:
                # A、B通道都降低num
                await client.set_strength(Channel.A, StrengthOperationType.DECREASE, num)
                await client.set_strength(Channel.B, StrengthOperationType.DECREASE, num)
                msg_list.append(f"玩家{user_id_map.get(user_id, user_id)}双通道降低了{num}档位")

        # 发送汇总消息
        await self.send_message(f"本次全体随机降低由{user_id_map.get(self.qq_id, self.qq_id)}执行\r\n本次全体随机降低结果(按档位升序):\r\n" + "\r\n".join(msg_list))
        _log.info(f"用户{self.qq_id} 执行全体随机降低，结果：{','.join(msg_list)}")


    async def random_decrease(self):
        # 必须是已连接用户才有权使用
        user_conn = user_manager.get_user_connection(self.qq_id)
        if user_conn['status'] != 'connected' or not user_conn.get('commander') or not getattr(user_conn['commander'], 'client', None):
            await self.send_message("只有已连接的用户才可以使用随机降低命令")
            return

        # 支持：/随机降低 20 或 /随机降低 A 20 或 /随机降低 B 20
        users = user_manager.get_all_users()
        # 过滤出已连接的用户
        connected_users = []
        for user_id in users:
            u_conn = user_manager.get_user_connection(user_id)
            if u_conn['status'] == 'connected' and u_conn.get('commander') and getattr(u_conn['commander'], 'client', None):
                connected_users.append(user_id)
        if not connected_users:
            await self.send_message("当前没有已连接的用户，无法执行随机降低")
            return
        # 随机选一个用户
        target_user_id = random.choice(connected_users)
        target_user_name = user_id_map.get(target_user_id, target_user_id)
        target_user_conn = user_manager.get_user_connection(target_user_id)
        target_commander = target_user_conn.get('commander')
        target_client = getattr(target_commander, 'client', None)
        if not target_client:
            await self.send_message("随机选中的用户未正确连接，操作失败")
            return

        param_kwargs = self.kwargs.copy()
        # 只允许1或2个参数
        if len(param_kwargs) == 1 and param_kwargs[0].isdigit():
            value = int(param_kwargs[0])
            await target_client.set_strength(Channel.A, StrengthOperationType.DECREASE, value)
            await target_client.set_strength(Channel.B, StrengthOperationType.DECREASE, value)
            await self.send_message(f"已随机为用户{target_user_name}降低A通道{value}，B通道{value}\r\nA当前:{self.strength.a},A上限{self.strength.a_limit}B当前:{self.strength.b},B上限{self.strength.b_limit}")
            _log.info(f"用户{self.qq_id}随机为{target_user_name}降低A:{value} B:{value}")
            return
        if len(param_kwargs) == 2 and param_kwargs[0] in {'A', 'B'} and param_kwargs[1].isdigit():
            channel = param_kwargs[0]
            value = int(param_kwargs[1])
            if channel == 'A':
                await target_client.set_strength(Channel.A, StrengthOperationType.DECREASE, value)
            else:
                await target_client.set_strength(Channel.B, StrengthOperationType.DECREASE, value)
            await self.send_message(f"已随机为用户{target_user_name}降低{channel}通道{value}")
            _log.info(f"用户{self.qq_id}随机为{target_user_name}降低{channel}:{value}")
            return
        try:
            await self.send_message('随机降低命令格式错误，应为“/随机降低 20”或“/随机降低 A 20”')
        except RuntimeError:
            pass
        _log.error(f'用户{self.qq_id} 随机降低命令格式错误')

    async def status(self):
        _log.info(f'用户{self.qq_id} status命令执行')
        if self.size != 1:
            await self.send_message('status命令不应有参数')
            return

        users = user_manager.get_all_users()
        if not users:
            await self.send_message("当前没有用户")
            return

        msg = "所有用户状态：\r\n"
        for user_id in users:
            user_conn = user_manager.get_user_connection(user_id)
            commander = user_conn.get('commander')
            user_name = user_id_map.get(user_id, user_id)
            status_code = getattr(commander, 'status_code', 0)
            if status_code == 0:
                status_text = '未连接'
                msg += f"{user_name}：{status_text}\r\n"
            elif status_code == 1:
                status_text = '等待连接'
                msg += f"{user_name}：{status_text}\r\n"
            elif status_code == 2:
                strength = getattr(commander, 'strength', None)
                current_pulses_A = getattr(commander, 'current_pulses_A_name', '')
                current_pulses_B = getattr(commander, 'current_pulses_B_name', '')
                if strength:
                    msg += (f"{user_name}：已连接\r\n"
                            f"  A通道：{strength.a} 上限{strength.a_limit}\r\n"
                            f"  B通道：{strength.b} 上限{strength.b_limit}\r\n"
                            f"  A波形：{current_pulses_A}, B波形{current_pulses_B}\r\n")
                else:
                    msg += f"{user_name}：已连接（无强度数据）\r\n"
            else:
                msg += f"{user_name}：未知状态\n"

        await self.send_message(msg)

    async def close(self):
        self.close_tag = True
        self.pulse_close_tag = True
        _log.info(f'用户{self.qq_id} close_tag已发送')
        await self.send_message("已发送断开连接信号，可能需要较长时间响应")

    async def user_list(self):
        """显示所有用户列表（支持@用户）"""
        users = user_manager.get_all_users()
        if not users:
            await self.send_message("当前没有用户连接")
            return

        user_list_msg = "当前用户列表：\n"
        for user_id in users:
            user_conn = user_manager.get_user_connection(user_id)
            status_text = {
                'disconnected': '未连接',
                'connecting': '连接中',
                'connected': '已连接'
            }.get(user_conn['status'], '未知状态')
            if user_conn['status'] == 'disconnected':
                # 未连接的用户不显示
                continue
            user_list_msg += f"{user_id_map.get(user_id, user_id)} - 状态: {status_text}\n"

        await self.send_message(user_list_msg)

    async def get_my_user_id(self):
        await self.send_message(self.qq_id)


    async def help(self):
        await self.send_message('这里是命令介绍喵~\r\n\r\n'
                                '「新建连接」命令用于连接app，无参数，每个用户都有独立的连接\r\n'
                                '「设置强度」,「增加强度」,「降低强度」命令用于设定、增加、减小所有已连接用户的强度，如：设置强度 A 100或 设置强度 100（同时设置双通道）\r\n'
                                '也支持@指定用户，如：设置强度 100 @呱呱糕、增加强度 A 20 @呱呱糕\r\n'
                                '「随机增加」,「随机降低」命令仅限已连接用户使用，格式如：/随机增加 20 或 /随机增加 A 20\r\n'
                                '「当前状态」命令用于查看当前连接状况，强度大小，强度上限，无参数\r\n'
                                '「改变波形」命令用于更改指定通道波形，如：改变波形 A 潮汐，波形名称列表如下：\r\n'
                                '呼吸、潮汐、连击、快速按捏、按捏渐强、心跳节奏、压缩、节奏步伐、颗粒摩擦、渐变弹跳、波浪涟漪、雨水冲刷、变速敲击、信号灯、挑逗1、挑逗2\r\n'
                                '「用户列表」命令用于查看所有当前用户及其连接状态\r\n'
                                '「全体随机增加」,「全体随机降低」命令用于随机增加或降低所有已连接用户的强度，如：全体随机增加 100 或 全体随机降低 100\r\n'
                                '「获取ID」命令用于获取当前用户的ID,获取后联系呱呱糕添加进去\r\n'
                                '「帮助」命令用于查看所有命令\r\n'
                                )


class MyClient(botpy.Client):
    async def on_ready(self):
        _log.info(f"robot 「{self.robot.name}」 准备就绪！")

    async def on_group_at_message_create(self, message: GroupMessage):
        qq_id = message.author.member_openid
        user_name = qq_id
        
        # 获取或创建用户的连接管理器，并存储用户名称
        user_conn = user_manager.get_user_connection(qq_id, user_name=user_name)
        commander = user_conn['commander'] 
        
        # 处理消息
        await commander.reslove(message)


if __name__ == "__main__":
    # 通过预设置的类型，设置需要监听的事件通道
    # intents = botpy.Intents.none()
    # intents.public_messages=True

    # 通过kwargs，设置需要监听的事件通道
    intents = botpy.Intents(public_messages=True)
    client = MyClient(intents=intents)
    client.run(appid=test_config["appid"], secret=test_config["secret"])

