#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gemini语音聊天工具
支持通过语音输入与Gemini模型进行沟通
按 's' 开始录音，按 'e' 停止录音并发送给Gemini
"""

import os
import sys
import time
import wave
import tempfile
import asyncio
import threading
import termios
import tty
import select
from typing import Optional
import pyaudio
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# 音频配置
CHUNK = 1024
FORMAT = pyaudio.paInt16=======================================================================
CHANNELS = 1
RATE = 16000
AUDIO_FORMAT = "wav"

# Gemini配置
GOOGLE_API_KEY = "AIzaSyB2H34ibtQZGoHGeDgyJcM-kAKrXH4uSsQ"
MODEL_NAME = "gemini-2.5-flash"


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
            
        self.frames = []
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
            print("🎤 开始录音...")
            return True
        except Exception as e:
            print(f"❌ 录音启动失败: {e}")
            self.recording = False
            return False
        
    def stop_recording(self):
        """停止录音"""
        if not self.recording:
            return None
            
        self.recording = False
        print("⏹️  停止录音")
        
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None
            
        return self._save_audio()
    
    def _audio_callback(self, in_data, frame_count, time_info, status):
        """音频回调函数"""
        if self.recording:
            self.frames.append(in_data)
        return (in_data, pyaudio.paContinue)
    
    def _save_audio(self):
        """保存音频文件"""
        if not self.frames:
            return None
            
        # 创建临时文件
        temp_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        temp_filename = temp_file.name
        temp_file.close()
        
        try:
            # 保存为WAV文件
            with wave.open(temp_filename, 'wb') as wf:
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(self.audio.get_sample_size(FORMAT))
                wf.setframerate(RATE)
                wf.writeframes(b''.join(self.frames))
            
            print(f"💾 音频已保存: {temp_filename}")
            return temp_filename
            
        except Exception as e:
            print(f"❌ 保存音频失败: {e}")
            return None
    
    def cleanup(self):
        """清理资源"""
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.audio.terminate()


class GeminiVoiceChat:
    """Gemini语音聊天客户端"""
    
    def __init__(self):
        # 配置Gemini API
        genai.configure(api_key=GOOGLE_API_KEY)
        
        # 创建模型实例
        self.model = genai.GenerativeModel(MODEL_NAME)
        
        # 创建音频录制器
        self.recorder = AudioRecorder()
        
        # 安全设置
        self.safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
        }
        
    async def upload_audio(self, audio_file_path: str):
        """上传音频文件到Gemini"""
        try:
            print("📤 上传音频文件...")
            
            # 上传文件
            audio_file = genai.upload_file(
                path=audio_file_path,
                mime_type="audio/wav"
            )
            
            print("✅ 音频文件上传成功")
            return audio_file
            
        except Exception as e:
            print(f"❌ 上传音频失败: {e}")
            return None
    
    async def generate_response(self, audio_file, prompt: str = "你是一个ai智能助手，请根据音频内容回答或评论。"):
        """生成Gemini响应"""
        try:
            print("🤖 Gemini正在处理...")
            
            # 构建内容
            content = [prompt, audio_file]
            
            # 生成流式响应
            response = self.model.generate_content(
                content,
                safety_settings=self.safety_settings,
                stream=True
            )
            
            print("\n💬 Gemini回复:")
            print("-" * 50)
            
            full_response = ""
            try:
                for chunk in response:
                    # 检查chunk是否有text属性且不为空
                    if hasattr(chunk, 'text') and chunk.text:
                        print(chunk.text, end='', flush=True)
                        full_response += chunk.text
                    # 检查是否有candidates并且有内容
                    elif hasattr(chunk, 'candidates') and chunk.candidates:
                        for candidate in chunk.candidates:
                            if hasattr(candidate, 'content') and candidate.content:
                                if hasattr(candidate.content, 'parts') and candidate.content.parts:
                                    for part in candidate.content.parts:
                                        if hasattr(part, 'text') and part.text:
                                            print(part.text, end='', flush=True)
                                            full_response += part.text
                            
            except Exception as stream_error:
                print(f"\n⚠️  流式响应处理中断: {stream_error}")
                # 尝试获取完整响应
                if full_response:
                    print("✅ 已获取部分响应内容")
                else:
                    print("❌ 未获取到响应内容")
                    
            print("\n" + "-" * 50)
            
            if full_response:
                return full_response
            else:
                print("⚠️  未获取到有效响应，可能是网络问题或API限制")
                return None
                
        except Exception as e:
            print(f"❌ 生成响应失败: {e}")
            return None
    
    def cleanup_temp_file(self, file_path: str):
        """清理临时文件"""
        try:
            if os.path.exists(file_path):
                os.unlink(file_path)
                print(f"🗑️  临时文件已删除: {file_path}")
        except Exception as e:
            print(f"⚠️  删除临时文件失败: {e}")
    
    async def start_chat(self):
        """开始语音聊天"""
        print("=" * 60)
        print("🎙️  Gemini语音聊天工具")
        print("=" * 60)
        print("使用说明:")
        print("• 按 's' 开始录音")
        print("• 按 'e' 停止录音并发送给Gemini")
        print("• 按 'q' 退出程序")
        print("-" * 60)
        
        with KeyboardInput() as kb:
            recording = False
            
            while True:
                try:
                    print("\n⌨️  等待输入 (s=开始录音, e=停止录音, q=退出)...")
                    
                    # 等待按键输入
                    while True:
                        char = kb.get_char()
                        if char:
                            break
                        await asyncio.sleep(0.1)
                    
                    if char == 'q':
                        print("👋 再见!")
                        break
                    elif char == 's' and not recording:
                        # 开始录音
                        if self.recorder.start_recording():
                            recording = True
                        else:
                            print("❌ 无法开始录音")
                    elif char == 'e' and recording:
                        # 停止录音并处理
                        audio_file_path = self.recorder.stop_recording()
                        recording = False
                        
                        if audio_file_path:
                            # 上传音频并获取响应
                            audio_file = await self.upload_audio(audio_file_path)
                            
                            if audio_file:
                                await self.generate_response(audio_file)
                            
                            # 清理临时文件
                            self.cleanup_temp_file(audio_file_path)
                        else:
                            print("❌ 没有录制到音频")
                    elif char == 'e' and not recording:
                        print("⚠️  请先按 's' 开始录音")
                    elif char == 's' and recording:
                        print("⚠️  正在录音中，请按 'e' 停止")
                        
                except KeyboardInterrupt:
                    print("\n👋 程序被中断，正在退出...")
                    break
                except Exception as e:
                    print(f"❌ 发生错误: {e}")
        
        # 清理资源
        self.recorder.cleanup()
        print("✅ 资源清理完成")


async def main():
    """主函数"""
    try:
        chat = GeminiVoiceChat()
        await chat.start_chat()
    except Exception as e:
        print(f"❌ 程序启动失败: {e}")
        print("请确保已安装所需依赖: pip install google-generativeai pyaudio")


def run_main():
    """运行主函数"""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 程序被中断，正在退出...")
    except Exception as e:
        print(f"❌ 运行时错误: {e}")


if __name__ == "__main__":
    run_main() 