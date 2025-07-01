#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
火山引擎大模型语音识别工具
基于官方文档实现的语音识别客户端
按 's' 开始录音，按 'e' 结束录音并识别
"""

import asyncio
import gzip
import json
import uuid
import threading
import signal
import sys
import termios
import tty
import select
import time
import websockets
import pyaudio
from io import BytesIO


# 火山引擎API配置
APPID = "3995332347"
ACCESS_TOKEN = "edFwMlXZa0ZadJE-FeLb4HHknCF0onmG"
SECRET_KEY = "qsDFpENAEIbNU_Y_ej4VdKH8s46vcPku"
WS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"

# 协议常量
PROTOCOL_VERSION = 0b0001
HEADER_SIZE = 0b0001
FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_REQUEST = 0b0010
FULL_SERVER_RESPONSE = 0b1001
SERVER_ERROR_RESPONSE = 0b1111

# 消息类型特定标志
NO_SEQUENCE = 0b0000
POS_SEQUENCE = 0b0001
NEG_SEQUENCE = 0b0010
NEG_WITH_SEQUENCE = 0b0011

# 序列化和压缩方式
JSON = 0b0001
GZIP = 0b0001
NO_COMPRESSION = 0b0000

# 音频配置
CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000


class KeyboardInput:
    """键盘输入监听器"""
    
    def __init__(self):
        self.old_settings = None
        
    def __enter__(self):
        """进入上下文时设置键盘为非缓冲模式"""
        self.old_settings = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())
        return self
        
    def __exit__(self, type, value, traceback):
        """退出上下文时恢复键盘设置"""
        if self.old_settings:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.old_settings)
    
    def get_char(self, timeout=0.1):
        """获取单个字符输入"""
        if select.select([sys.stdin], [], [], timeout):
            return sys.stdin.read(1).lower()
        return None


def generate_header(message_type=FULL_CLIENT_REQUEST, message_type_specific_flags=NO_SEQUENCE,
                   serial_method=JSON, compression_type=GZIP, reserved_data=0x00):
    """生成WebSocket协议头"""
    header = bytearray()
    header.append((PROTOCOL_VERSION << 4) | HEADER_SIZE)
    header.append((message_type << 4) | message_type_specific_flags)
    header.append((serial_method << 4) | compression_type)
    header.append(reserved_data)
    return header


def generate_payload_with_sequence(sequence: int):
    """生成带序列号的载荷前缀"""
    payload_prefix = bytearray()
    payload_prefix.extend(sequence.to_bytes(4, 'big', signed=True))
    return payload_prefix


def parse_response(res):
    """解析WebSocket响应"""
    if len(res) < 4:
        return {'error': 'Response too short'}
    
    protocol_version = res[0] >> 4
    header_size = res[0] & 0x0f
    message_type = res[1] >> 4
    message_type_specific_flags = res[1] & 0x0f
    serialization_method = res[2] >> 4
    message_compression = res[2] & 0x0f
    reserved = res[3]
    
    payload = res[header_size * 4:]
    result = {'is_last_package': False}
    payload_msg = None
    payload_size = 0
    
    # 检查是否包含序列号
    if message_type_specific_flags & 0x01:
        if len(payload) >= 4:
            seq = int.from_bytes(payload[:4], "big", signed=True)
            result['payload_sequence'] = seq
            payload = payload[4:]

    # 检查是否是最后一个包
    if message_type_specific_flags & 0x02:
        result['is_last_package'] = True

    # 根据消息类型解析载荷
    if message_type == FULL_SERVER_RESPONSE:
        if len(payload) >= 4:
            payload_size = int.from_bytes(payload[:4], "big", signed=True)
            payload_msg = payload[4:]
    elif message_type == SERVER_ERROR_RESPONSE:
        if len(payload) >= 8:
            code = int.from_bytes(payload[:4], "big", signed=False)
            result['code'] = code
            payload_size = int.from_bytes(payload[4:8], "big", signed=False)
            payload_msg = payload[8:]
            
    if payload_msg is None:
        return result
        
    try:
        # 解压缩
        if message_compression == GZIP:
            payload_msg = gzip.decompress(payload_msg)
            
        # 反序列化
        if serialization_method == JSON:
            payload_msg = json.loads(str(payload_msg, "utf-8"))
        elif serialization_method != 0:
            payload_msg = str(payload_msg, "utf-8")
            
        result['payload_msg'] = payload_msg
        result['payload_size'] = payload_size
    except Exception as e:
        result['parse_error'] = str(e)
        
    return result


class AudioRecorder:
    """音频录制器"""
    
    def __init__(self):
        self.audio = pyaudio.PyAudio()
        self.frames = []
        self.recording = False
        self.stream = None
        
    def start_recording(self):
        """开始录音"""
        if self.recording:
            return False
            
        self.recording = True
        self.frames = []
        
        try:
            self.stream = self.audio.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                frames_per_buffer=CHUNK,
                stream_callback=self._audio_callback
            )
            self.stream.start_stream()
            print("🎤 开始录音... (按 'e' 结束录音)")
            return True
        except Exception as e:
            print(f"❌ 录音启动失败: {e}")
            self.recording = False
            return False
        
    def stop_recording(self):
        """停止录音"""
        if not self.recording:
            return b''
            
        self.recording = False
        
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None
            
        audio_data = b''.join(self.frames)
        print(f"🛑 录音结束，录制了 {len(audio_data)} 字节数据")
        return audio_data
    
    def _audio_callback(self, in_data, frame_count, time_info, status):
        """音频回调函数"""
        if self.recording:
            self.frames.append(in_data)
        return (None, pyaudio.paContinue)
        
    def cleanup(self):
        """清理资源"""
        if self.recording:
            self.stop_recording()
        self.audio.terminate()


class BigModelASRClient:
    """火山引擎大模型语音识别客户端"""
    
    def __init__(self):
        self.seg_duration = 100  # 分段持续时间（毫秒）
        
    def construct_request(self, reqid):
        """构建识别请求参数"""
        return {
            "user": {
                "uid": "bigmodel_asr_user"
            },
            "audio": {
                'format': 'pcm',
                "sample_rate": RATE,
                "bits": 16,
                "channel": CHANNELS,
                "codec": 'raw',
            },
            "request": {
                "model_name": "bigmodel",
                "enable_punc": True,  # 启用标点符号
                "enable_itn": True,   # 启用逆文本规范化
            }
        }

    async def recognize_audio(self, audio_data):
        """语音识别主函数"""
        if not audio_data:
            return {"error": "没有音频数据"}
            
        reqid = str(uuid.uuid4())
        seq = 1
        
        print(f"🔄 开始语音识别...")
        
        # 构建请求参数
        request_params = self.construct_request(reqid)
        payload_bytes = str.encode(json.dumps(request_params))
        payload_bytes = gzip.compress(payload_bytes)
        
        # 构建完整客户端请求
        full_client_request = bytearray(generate_header(message_type_specific_flags=POS_SEQUENCE))
        full_client_request.extend(generate_payload_with_sequence(sequence=seq))
        full_client_request.extend((len(payload_bytes)).to_bytes(4, 'big'))
        full_client_request.extend(payload_bytes)
        
        # 设置请求头
        headers = {
            "X-Api-Resource-Id": "volc.bigasr.sauc.duration",
            "X-Api-Access-Key": ACCESS_TOKEN,
            "X-Api-App-Key": APPID,
            "X-Api-Request-Id": reqid
        }
        
        try:
            async with websockets.connect(
                WS_URL, 
                additional_headers=headers, 
                max_size=1000000000
            ) as ws:
                print("✅ WebSocket连接成功")
                
                # 发送初始请求
                await ws.send(full_client_request)
                res = await ws.recv()
                result = parse_response(res)
                
                if 'error' in result:
                    return result
                
                if 'payload_msg' in result and 'code' in result['payload_msg']:
                    if result['payload_msg']['code'] != 1000:
                        return {"error": f"服务器错误: {result['payload_msg']}"}
                
                # 计算分段大小
                segment_size = int(RATE * 2 * CHANNELS * self.seg_duration / 1000)
                chunks = [audio_data[i:i+segment_size] for i in range(0, len(audio_data), segment_size)]
                total_chunks = len(chunks)
                
                print(f"📦 分为 {total_chunks} 个数据包发送")
                
                recognition_results = []
                
                # 发送音频数据
                for i, chunk in enumerate(chunks):
                    seq += 1
                    is_last = (i == len(chunks) - 1)
                    
                    if is_last:
                        seq = -seq  # 最后一个包使用负序列号
                    
                    # 压缩音频数据
                    payload_bytes = gzip.compress(chunk)
                    
                    # 构建音频请求
                    if is_last:
                        audio_request = bytearray(generate_header(
                            message_type=AUDIO_ONLY_REQUEST, 
                            message_type_specific_flags=NEG_WITH_SEQUENCE
                        ))
                    else:
                        audio_request = bytearray(generate_header(
                            message_type=AUDIO_ONLY_REQUEST, 
                            message_type_specific_flags=POS_SEQUENCE
                        ))
                    
                    audio_request.extend(generate_payload_with_sequence(sequence=seq))
                    audio_request.extend((len(payload_bytes)).to_bytes(4, 'big'))
                    audio_request.extend(payload_bytes)
                    
                    # 发送音频数据
                    await ws.send(audio_request)
                    res = await ws.recv()
                    result = parse_response(res)
                    
                    # 处理识别结果
                    if 'payload_msg' in result and result['payload_msg']:
                        msg = result['payload_msg']
                        if 'result' in msg and msg['result']:
                            text = msg['result'].get('text', '')
                            if text:
                                recognition_results.append(text)
                                print(f"📝 识别片段: {text}")
                    
                    # 显示进度
                    progress = int((i + 1) / total_chunks * 100)
                    print(f"⏳ 处理进度: {progress}%")
                
                # 返回最终结果
                final_text = ' '.join(recognition_results) if recognition_results else ""
                return {
                    "success": True,
                    "text": final_text,
                    "chunks_processed": total_chunks
                }
                
        except websockets.exceptions.ConnectionClosedError as e:
            error_msg = f"WebSocket连接关闭: {e.code} - {e.reason}"
            print(f"❌ {error_msg}")
            return {"error": error_msg}
        except Exception as e:
            error_msg = f"识别过程出错: {str(e)}"
            print(f"❌ {error_msg}")
            return {"error": error_msg}


def signal_handler(signum, frame):
    """信号处理器"""
    print("\n🛑 收到中断信号，正在退出...")
    sys.exit(0)


def main():
    """主函数"""
    print("🌋 火山引擎大模型语音识别工具")
    print("=" * 50)
    print("📋 控制说明:")
    print("  按 's' 键 - 开始录音")
    print("  按 'e' 键 - 结束录音并识别")
    print("  按 'q' 键 - 退出程序")
    print("=" * 50)
    
    # 设置信号处理
    signal.signal(signal.SIGINT, signal_handler)
    
    # 检查依赖
    try:
        import pyaudio
    except ImportError:
        print("❌ 错误: 请先安装 pyaudio")
        print("📦 安装命令: pip install pyaudio")
        print("🍎 macOS用户可能需要: brew install portaudio")
        return
        
    try:
        import websockets
    except ImportError:
        print("❌ 错误: 请先安装 websockets")
        print("📦 安装命令: pip install websockets")
        return
    
    # 初始化组件
    recorder = AudioRecorder()
    client = BigModelASRClient()
    
    try:
        with KeyboardInput() as kb:
            print("\n💡 准备就绪，等待按键...")
            
            while True:
                char = kb.get_char()
                
                if char == 's':
                    if not recorder.recording:
                        if recorder.start_recording():
                            pass  # 开始录音的消息已在 start_recording 中打印
                        else:
                            print("❌ 录音启动失败")
                    else:
                        print("⚠️  已在录音中...")
                        
                elif char == 'e':
                    if recorder.recording:
                        # 停止录音并获取数据
                        audio_data = recorder.stop_recording()
                        
                        if audio_data:
                            print("🔄 正在进行语音识别...")
                            # 进行语音识别
                            result = asyncio.run(client.recognize_audio(audio_data))
                            
                            if 'error' in result:
                                print(f"❌ 识别失败: {result['error']}")
                            elif result.get('success'):
                                print("\n" + "="*60)
                                print("✅ 识别成功!")
                                print(f"📄 识别结果: {result['text']}")
                                print(f"📊 处理了 {result['chunks_processed']} 个数据包")
                                print("="*60)
                                print("\n💡 按 's' 开始下一次录音...")
                            else:
                                print("❌ 识别失败: 未知错误")
                        else:
                            print("❌ 没有录制到音频数据")
                    else:
                        print("⚠️  当前未在录音，请先按 's' 开始录音")
                        
                elif char == 'q':
                    print("👋 退出程序...")
                    break
                elif char and char not in ['\n', '\r']:
                    print(f"❓ 未知按键 '{char}'，请按 's' 开始录音，'e' 结束录音，'q' 退出")
                    
    except KeyboardInterrupt:
        print("\n🛑 程序被中断")
    except Exception as e:
        print(f"❌ 程序运行错误: {e}")
    finally:
        recorder.cleanup()
        print("🧹 资源清理完成")


if __name__ == '__main__':
    main() 