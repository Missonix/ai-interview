#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
火山引擎语音识别工具 - 实时识别版本
支持在macOS上实时录制语音并通过火山引擎大模型接口转换为文字
按 's' 开始实时录音识别，按 'e' 结束录音识别
"""

import asyncio
import datetime
import gzip
import json
import time
import uuid
import threading
import signal
import sys
import termios
import tty
import select
import queue
from io import BytesIO
import pyaudio
import websockets


# 协议常量
PROTOCOL_VERSION = 0b0001
FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_REQUEST = 0b0010
FULL_SERVER_RESPONSE = 0b1001
SERVER_ACK = 0b1011
SERVER_ERROR_RESPONSE = 0b1111

POS_SEQUENCE = 0b0001
NEG_WITH_SEQUENCE = 0b0011
JSON = 0b0001
GZIP = 0b0001

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


def generate_header(message_type=FULL_CLIENT_REQUEST, message_type_specific_flags=0,
                   serial_method=JSON, compression_type=GZIP, reserved_data=0x00):
    """生成WebSocket协议头"""
    header = bytearray()
    header_size = 1
    header.append((PROTOCOL_VERSION << 4) | header_size)
    header.append((message_type << 4) | message_type_specific_flags)
    header.append((serial_method << 4) | compression_type)
    header.append(reserved_data)
    return header


def generate_before_payload(sequence: int):
    """生成载荷前数据"""
    before_payload = bytearray()
    before_payload.extend(sequence.to_bytes(4, 'big', signed=True))
    return before_payload


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
    
    if message_type_specific_flags & 0x01:
        if len(payload) >= 4:
            seq = int.from_bytes(payload[:4], "big", signed=True)
            result['payload_sequence'] = seq
            payload = payload[4:]

    if message_type_specific_flags & 0x02:
        result['is_last_package'] = True

    if message_type == FULL_SERVER_RESPONSE:
        if len(payload) >= 4:
            payload_size = int.from_bytes(payload[:4], "big", signed=True)
            payload_msg = payload[4:]
    elif message_type == SERVER_ACK:
        if len(payload) >= 4:
            seq = int.from_bytes(payload[:4], "big", signed=True)
            result['seq'] = seq
            if len(payload) >= 8:
                payload_size = int.from_bytes(payload[4:8], "big", signed=False)
                payload_msg = payload[8:]
    elif message_type == SERVER_ERROR_RESPONSE:
        if len(payload) >= 8:
            code = int.from_bytes(payload[:4], "big", signed=False)
            result['code'] = code
            payload_size = int.from_bytes(payload[4:8], "big", signed=False)
            payload_msg = payload[8:]
            
    if payload_msg is None:
        return result
        
    try:
        if message_compression == GZIP:
            payload_msg = gzip.decompress(payload_msg)
            
        if serialization_method == JSON:
            payload_msg = json.loads(str(payload_msg, "utf-8"))
        elif serialization_method != 0:
            payload_msg = str(payload_msg, "utf-8")
            
        result['payload_msg'] = payload_msg
        result['payload_size'] = payload_size
    except Exception as e:
        result['parse_error'] = str(e)
        
    return result


class RealtimeAudioRecorder:
    """实时音频录制器"""
    
    def __init__(self):
        self.audio = pyaudio.PyAudio()
        self.audio_queue = queue.Queue()
        self.recording = False
        self.stream = None
        
    def start_recording(self):
        """开始录音"""
        if self.recording:
            return False
            
        self.recording = True
        
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
            return True
        except Exception as e:
            print(f"❌ 录音启动失败: {e}")
            self.recording = False
            return False
        
    def stop_recording(self):
        """停止录音"""
        if not self.recording:
            return
            
        self.recording = False
        
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None
    
    def get_audio_chunk(self, timeout=0.1):
        """获取音频数据块"""
        try:
            return self.audio_queue.get(timeout=timeout)
        except queue.Empty:
            return None
    
    def _audio_callback(self, in_data, frame_count, time_info, status):
        """音频回调函数"""
        if self.recording:
            self.audio_queue.put(in_data)
        return (None, pyaudio.paContinue)
        
    def cleanup(self):
        """清理资源"""
        if self.recording:
            self.stop_recording()
        self.audio.terminate()


class RealtimeVoiceRecognitionClient:
    """实时语音识别客户端"""
    
    def __init__(self):
        self.ws_url = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
        self.app_id = "3995332347"
        self.access_token = "edFwMlXZa0ZadJE-FeLb4HHknCF0onmG"
        self.seg_duration = 100  # ms
        self.recognized_text = ""  # 累积的识别文本
        self.ws = None
        self.seq = 1
        self.recognition_active = False
        
    def construct_request(self, reqid):
        """构建识别请求"""
        return {
            "user": {"uid": "macos_user"},
            "audio": {
                'format': 'pcm',
                "sample_rate": RATE,
                "bits": 16,
                "channel": CHANNELS,
                "codec": 'raw',
            },
            "request": {
                "model_name": "bigmodel",
                "enable_punc": True,
                "enable_itn": True,
            }
        }

    async def start_recognition(self, audio_recorder):
        """开始实时识别"""
        self.recognized_text = ""
        self.seq = 1
        self.recognition_active = True
        
        reqid = str(uuid.uuid4())
        
        print(f"🔄 开始实时语音识别...")
        print("📝 识别结果:")
        print("-" * 60)
        
        # 构建请求参数
        request_params = self.construct_request(reqid)
        payload_bytes = str.encode(json.dumps(request_params))
        payload_bytes = gzip.compress(payload_bytes)
        
        # 构建完整客户端请求
        full_client_request = bytearray(generate_header(message_type_specific_flags=POS_SEQUENCE))
        full_client_request.extend(generate_before_payload(sequence=self.seq))
        full_client_request.extend((len(payload_bytes)).to_bytes(4, 'big'))
        full_client_request.extend(payload_bytes)
        
        # 设置请求头
        headers = {
            "X-Api-Resource-Id": "volc.bigasr.sauc.duration",
            "X-Api-Access-Key": self.access_token,
            "X-Api-App-Key": self.app_id,
            "X-Api-Request-Id": reqid
        }
        
        try:
            self.ws = await websockets.connect(
                self.ws_url, 
                additional_headers=headers, 
                max_size=1000000000
            )
            
            print("✅ WebSocket连接成功，开始实时识别...")
            
            # 发送初始请求
            await self.ws.send(full_client_request)
            res = await self.ws.recv()
            result = parse_response(res)
            
            if 'error' in result:
                print(f"❌ 初始化失败: {result['error']}")
                return
            
            # 启动音频发送和结果接收任务
            send_task = asyncio.create_task(self._send_audio_data(audio_recorder))
            recv_task = asyncio.create_task(self._receive_recognition_results())
            
            # 等待任务完成
            await asyncio.gather(send_task, recv_task, return_exceptions=True)
            
        except Exception as e:
            print(f"❌ 识别过程出错: {str(e)}")
        finally:
            if self.ws:
                await self.ws.close()
                self.ws = None

    async def _send_audio_data(self, audio_recorder):
        """发送音频数据"""
        try:
            while self.recognition_active and audio_recorder.recording:
                # 获取音频数据
                audio_chunk = audio_recorder.get_audio_chunk(timeout=0.1)
                if audio_chunk is None:
                    continue
                
                self.seq += 1
                
                # 压缩音频数据
                payload_bytes = gzip.compress(audio_chunk)
                
                # 构建音频请求
                audio_request = bytearray(generate_header(
                    message_type=AUDIO_ONLY_REQUEST, 
                    message_type_specific_flags=POS_SEQUENCE
                ))
                
                audio_request.extend(generate_before_payload(sequence=self.seq))
                audio_request.extend((len(payload_bytes)).to_bytes(4, 'big'))
                audio_request.extend(payload_bytes)
                
                # 发送音频数据
                if self.ws:
                    await self.ws.send(audio_request)
                
                # 控制发送频率
                await asyncio.sleep(0.1)
                
        except Exception as e:
            print(f"❌ 音频发送错误: {str(e)}")

    async def _receive_recognition_results(self):
        """接收识别结果"""
        try:
            while self.recognition_active and self.ws:
                try:
                    res = await asyncio.wait_for(self.ws.recv(), timeout=1.0)
                    result = parse_response(res)
                    
                    # 处理识别结果
                    if 'payload_msg' in result and result['payload_msg']:
                        msg = result['payload_msg']
                        if 'result' in msg and msg['result']:
                            text = msg['result'].get('text', '').strip()
                            if text:
                                # 更新累积文本
                                self._update_recognized_text(text)
                                
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    if self.recognition_active:
                        print(f"❌ 接收结果错误: {str(e)}")
                    break
                    
        except Exception as e:
            print(f"❌ 结果接收错误: {str(e)}")

    def _update_recognized_text(self, new_text):
        """更新识别文本并实时显示"""
        # 简单的文本拼接策略
        if not self.recognized_text:
            self.recognized_text = new_text
        else:
            # 检查新文本是否是对前文的扩展
            if new_text.startswith(self.recognized_text):
                self.recognized_text = new_text
            elif not any(word in self.recognized_text for word in new_text.split() if len(word) > 1):
                # 如果新文本与已有文本没有重叠，则拼接
                self.recognized_text += " " + new_text
            else:
                # 否则用新文本替换（可能是更准确的识别结果）
                self.recognized_text = new_text
        
        # 实时显示当前识别结果
        self._display_current_text()

    def _display_current_text(self):
        """显示当前识别文本"""
        # 清除当前行并显示最新文本
        print(f"\r🎤 实时识别: {self.recognized_text}", end="", flush=True)

    async def stop_recognition(self):
        """停止识别"""
        self.recognition_active = False
        
        if self.ws:
            try:
                # 发送结束信号
                self.seq = -self.seq
                
                # 发送空的结束帧
                audio_request = bytearray(generate_header(
                    message_type=AUDIO_ONLY_REQUEST, 
                    message_type_specific_flags=NEG_WITH_SEQUENCE
                ))
                
                audio_request.extend(generate_before_payload(sequence=self.seq))
                audio_request.extend((0).to_bytes(4, 'big'))
                
                await self.ws.send(audio_request)
                
                # 等待一小段时间接收最后的结果
                await asyncio.sleep(0.5)
                
            except Exception as e:
                pass
            
            await self.ws.close()
            self.ws = None
        
        # 显示最终结果
        print("\n" + "-" * 60)
        print("✅ 识别完成!")
        if self.recognized_text:
            print(f"📄 最终识别结果: 「{self.recognized_text}」")
        else:
            print("⚠️  未识别到有效文本内容")
        print("=" * 60)


def signal_handler(signum, frame):
    """信号处理器"""
    print("\n🛑 收到中断信号，正在退出...")
    sys.exit(0)


async def main_async():
    """异步主函数"""
    print("🌋 火山引擎实时语音识别工具")
    print("=" * 50)
    print("📋 控制说明:")
    print("  按 's' 键 - 开始实时录音识别")
    print("  按 'e' 键 - 结束录音识别")
    print("  按 'q' 键 - 退出程序")
    print("=" * 50)
    
    # 初始化组件
    recorder = RealtimeAudioRecorder()
    client = RealtimeVoiceRecognitionClient()
    
    try:
        with KeyboardInput() as kb:
            print("\n💡 准备就绪，等待按键...")
            
            while True:
                char = kb.get_char()
                
                if char == 's':
                    if not recorder.recording:
                        print("🎤 开始实时录音识别...")
                        if recorder.start_recording():
                            # 启动实时识别
                            await client.start_recognition(recorder)
                        else:
                            print("❌ 录音启动失败")
                    else:
                        print("⚠️  已在录音识别中...")
                        
                elif char == 'e':
                    if recorder.recording:
                        print("\n🛑 结束录音识别...")
                        
                        # 停止识别
                        await client.stop_recognition()
                        
                        # 停止录音
                        recorder.stop_recording()
                        
                        print("\n💡 按 's' 开始下一次录音识别...")
                    else:
                        print("⚠️  当前未在录音，请先按 's' 开始录音识别")
                        
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
        # 确保停止识别和录音
        if client.recognition_active:
            await client.stop_recognition()
        recorder.cleanup()
        print("🧹 资源清理完成")


def main():
    """主函数"""
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
    
    # 运行异步主函数
    asyncio.run(main_async())


if __name__ == '__main__':
    main() 