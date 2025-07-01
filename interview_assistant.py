#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
面试助手Web应用
基于语音输入的面试回答实时生成工具
"""

import os
import json
import base64
import tempfile
import asyncio
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from flask_socketio import SocketIO, emit
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import wave
import threading
import queue
from pydub import AudioSegment
from pydub.utils import which

# Gemini配置
GOOGLE_API_KEY = "AIzaSyB2H34ibtQZGoHGeDgyJcM-kAKrXH4uSsQ"
MODEL_NAME = "gemini-2.5-flash"

app = Flask(__name__)
app.config['SECRET_KEY'] = 'interview_assistant_secret_key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///interview_assistant.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# 初始化扩展
db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
# 增加消息大小限制以支持音频文件传输
socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    logger=False,  # 关闭详细日志减少延迟
    engineio_logger=False,  # 关闭engineio日志
    max_http_buffer_size=10000000,  # 10MB缓冲区大小
    ping_timeout=300,  # 增加ping超时时间到5分钟
    ping_interval=60,   # ping间隔增加到60秒
    async_mode='threading'  # 使用线程模式提高性能
)

# 配置Gemini API
genai.configure(api_key=GOOGLE_API_KEY)


# 数据库模型
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # 关系
    configs = db.relationship('InterviewConfig', backref='user', lazy=True, cascade='all, delete-orphan')
    voice_records = db.relationship('VoiceRecord', backref='user', lazy=True, cascade='all, delete-orphan')
    transcripts = db.relationship('InterviewTranscript', backref='user', lazy=True, cascade='all, delete-orphan')
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'created_at': self.created_at.isoformat()
        }


class InterviewConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)  # 配置名称
    candidate_name = db.Column(db.String(100), nullable=False)  # 姓名
    position = db.Column(db.String(100), nullable=False)  # 专业背景
    company = db.Column(db.String(100), nullable=False)  # 目标公司
    job_title = db.Column(db.String(100), nullable=False)  # 面试岗位
    resume = db.Column(db.Text)  # 简历概要
    detailed_experience = db.Column(db.Text)  # 详细工作经历
    job_description = db.Column(db.Text)  # 岗位JD
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'candidate_name': self.candidate_name,
            'position': self.position,
            'company': self.company,
            'job_title': self.job_title,
            'resume': self.resume,
            'detailed_experience': self.detailed_experience,
            'job_description': self.job_description,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat()
        }


class VoiceRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    config_id = db.Column(db.Integer, db.ForeignKey('interview_config.id'), nullable=True)
    question = db.Column(db.Text)  # 识别出的问题
    answer = db.Column(db.Text)    # AI生成的回答
    status = db.Column(db.String(20), default='generating')  # generating, completed, interrupted
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    is_read = db.Column(db.Boolean, default=False)  # 是否已读
    
    # 添加关系定义
    config = db.relationship('InterviewConfig', backref='voice_records', lazy=True)


class InterviewTranscript(db.Model):
    """面试逐字稿数据模型"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    config_id = db.Column(db.Integer, db.ForeignKey('interview_config.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)  # 逐字稿标题
    content = db.Column(db.Text, nullable=False)  # 逐字稿内容
    status = db.Column(db.String(20), default='generating')  # generating, completed, failed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    
    # 关系
    config = db.relationship('InterviewConfig', backref='transcripts', lazy=True)
    
    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'content': self.content,
            'status': self.status,
            'created_at': self.created_at.isoformat(),
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'config_name': self.config.name if self.config else None,
            'company': self.config.company if self.config else None,
            'job_title': self.config.job_title if self.config else None
        }


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


class InterviewAssistant:
    """面试助手核心类"""
    
    def __init__(self):
        # 初始化Gemini API
        genai.configure(api_key=GOOGLE_API_KEY)
        
        # 配置安全设置
        self.safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        ]
        
        # 创建模型实例
        self.model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",  # 使用2.5版本，对音频支持更好
            safety_settings=self.safety_settings
        )
        
        # 存储当前处理的记录ID
        self.current_record_id = None
        self.current_session_id = None
    
    def generate_system_prompt(self, user_info):
        """生成系统提示词"""
        return f"""你是{user_info.get('name', '李沛霖')}，一个资深的{user_info.get('position', '技术专家')}，现在正在面试{user_info.get('company', '知名公司')}的{user_info.get('job_title', '目标岗位')}。

个人背景：
- 姓名：{user_info.get('name', '李沛霖')}
- 专业背景：{user_info.get('position', '技术专家')}

简历概要：
{user_info.get('resume', '拥有丰富的相关工作经验')}

详细工作经历：
{user_info.get('detailed_experience', '请基于提供的简历概要展开详细经历')}

面试岗位：
{user_info.get('job_title', '目标岗位')}

岗位JD：
{user_info.get('job_description', '相关技术岗位')}

**核心回答要求**：
1. 直接回答面试官的问题，不要添加任何解释性开头
2. 不要说"好的，针对这个问题..."等套话
3. 不要添加"面试官："或"{user_info.get('name', '李沛霖')}："等格式化标识

**回答结构要求**：
4. **第一部分**：直接回答问题的核心要点，提供清晰的技术方法论或解决方案
5. **第二部分**：从详细工作经历中选择1-2个最相关的项目经历来佐证，要简洁明了
6. **结构化表达**：使用"首先...其次...最后..."或"从XX角度...从XX角度..."等逻辑词

**内容要求**：
7. 答案要聚焦问题本身，不要泛泛而谈
8. 技术细节要准确专业，体现深度理解
9. 个人经历只作为佐证，不要喧宾夺主
10. 语言口语化但保持专业性
11. 控制篇幅，重点突出
12. 使用Markdown格式，包含适当的标题、列表等

**避免的问题**：
- 不要每个技术点都穿插经历
- 不要过度展示项目，要服务于回答问题
- 不要结构混乱，技术和经历要分层表达
- 不要空话套话，要有实际内容


现在请按照上述要求直接回答面试官的语音问题："""

    async def process_audio_and_generate_response(self, audio_data, user_info, session_id, user_id=None, config_id=None):
        """处理音频并生成回答"""
        temp_filename = None
        voice_record = None
        
        try:
            print(f"=== 开始处理音频数据 ===")
            print(f"音频数据长度: {len(audio_data) if audio_data else 0}")
            print(f"Session ID: {session_id}")
            print(f"User ID: {user_id}")
            print(f"Config ID: {config_id}")
            print(f"用户信息: {user_info}")
            
            # 发送处理开始通知
            try:
                socketio.emit('processing_status', {
                    'stage': 'audio_upload',
                    'message': '正在上传音频文件...',
                    'progress': 10
                }, to=session_id)
            except Exception as emit_error:
                print(f"发送状态通知失败: {emit_error}")
            
            # 如果有正在处理的记录，标记为中断
            if self.current_record_id and user_id:
                try:
                    old_record = VoiceRecord.query.get(self.current_record_id)
                    if old_record and old_record.status == 'generating':
                        old_record.status = 'interrupted'
                        db.session.commit()
                        print(f"标记之前的记录 {self.current_record_id} 为中断状态")
                        
                        # 发送通知给前端
                        socketio.emit('voice_record_interrupted', {
                            'record_id': self.current_record_id,
                            'message': '之前的语音生成已中断，新内容已保存到历史记录'
                        }, to=self.current_session_id)
                        
                except Exception as e:
                    print(f"处理之前记录时出错: {e}")
            
            # 创建新的语音记录
            if user_id:
                voice_record = VoiceRecord(
                    user_id=user_id,
                    config_id=config_id,
                    status='generating'
                )
                db.session.add(voice_record)
                db.session.commit()
                
                self.current_record_id = voice_record.id
                self.current_session_id = session_id
                print(f"创建新的语音记录 ID: {voice_record.id}")
            
            # 检查音频数据格式
            if not audio_data or not audio_data.startswith('data:audio'):
                raise ValueError("无效的音频数据格式")
            
            # 检测音频格式
            audio_format = 'webm'  # 默认
            if 'audio/wav' in audio_data:
                audio_format = 'wav'
            elif 'audio/ogg' in audio_data:
                audio_format = 'ogg'
            elif 'audio/webm' in audio_data:
                audio_format = 'webm'
            
            print(f"检测到音频格式: {audio_format}")
            
            # 创建临时文件
            temp_file = tempfile.NamedTemporaryFile(suffix=f'.{audio_format}', delete=False)
            temp_filename = temp_file.name
            print(f"创建临时文件: {temp_filename}")
            
            # 解码base64音频数据
            try:
                print("开始解码base64音频数据...")
                # 移除data:audio/xxx;base64, 前缀
                if ',' in audio_data:
                    audio_base64 = audio_data.split(',')[1]
                else:
                    audio_base64 = audio_data
                    
                audio_bytes = base64.b64decode(audio_base64)
                print(f"解码后音频大小: {len(audio_bytes)} bytes")
                
                # 检查音频数据质量
                if len(audio_bytes) < 5000:  # 小于5KB可能质量不好
                    print(f"警告：音频数据较小 ({len(audio_bytes)} bytes)，可能录音时间太短")
                
                temp_file.write(audio_bytes)
                temp_file.close()
                print("音频数据写入临时文件完成")
                
            except Exception as decode_error:
                temp_file.close()
                print(f"音频解码失败: {decode_error}")
                raise ValueError(f"音频解码失败: {decode_error}")
            
            # 检查文件是否存在且不为空
            if not os.path.exists(temp_filename) or os.path.getsize(temp_filename) == 0:
                print(f"文件检查失败: 存在={os.path.exists(temp_filename)}, 大小={os.path.getsize(temp_filename) if os.path.exists(temp_filename) else 0}")
                raise ValueError("音频文件保存失败或为空")
            
            print(f"音频文件保存成功: {temp_filename}, 大小: {os.path.getsize(temp_filename)} bytes, 格式: {audio_format}")
            
            # 直接使用原始文件，根据格式设置正确的MIME类型
            upload_filename = temp_filename
            
            # 根据Gemini API文档设置正确的MIME类型
            mime_type_mapping = {
                'wav': 'audio/wav',
                'mp3': 'audio/mp3', 
                'ogg': 'audio/ogg',
                'webm': 'audio/webm',  # 虽然不在官方列表，但先尝试
                'aiff': 'audio/aiff',
                'aac': 'audio/aac',
                'flac': 'audio/flac'
            }
            
            preferred_mime_type = mime_type_mapping.get(audio_format, 'audio/wav')
            print(f"使用MIME类型: {preferred_mime_type}")
            
            # 验证上传文件
            if not os.path.exists(upload_filename) or os.path.getsize(upload_filename) == 0:
                raise ValueError("准备上传的音频文件无效")
            
            print(f"准备上传文件: {upload_filename}, 大小: {os.path.getsize(upload_filename)} bytes")
            
            # 上传音频文件到Gemini - 使用多种策略
            print("正在上传音频文件到Gemini...")
            audio_file = None
            upload_success = False
            
            # 策略1：使用检测到的MIME类型
            try:
                print(f"策略1 - 使用检测到的MIME类型: {preferred_mime_type}")
                
                audio_file = genai.upload_file(
                    path=upload_filename,
                    mime_type=preferred_mime_type,
                    display_name=f"interview_audio_{audio_format}"
                )
                print(f"策略1成功 - 音频文件上传成功: {audio_file.name}")
                upload_success = True
                
            except Exception as upload_error1:
                print(f"策略1失败: {upload_error1}")
                
                # 策略2：如果是WebM，尝试转换为OGG MIME类型
                if audio_format == 'webm':
                    try:
                        print("策略2 - WebM文件尝试使用OGG MIME类型...")
                        audio_file = genai.upload_file(
                            path=upload_filename,
                            mime_type='audio/ogg',
                            display_name="interview_audio_ogg"
                        )
                        print(f"策略2成功 - 音频文件上传成功: {audio_file.name}")
                        upload_success = True
                        
                    except Exception as upload_error2:
                        print(f"策略2失败: {upload_error2}")
                        
                        # 策略3：不指定MIME类型，让Gemini自动检测
                        try:
                            print("策略3 - 尝试自动检测MIME类型...")
                            audio_file = genai.upload_file(
                                upload_filename,
                                display_name="interview_audio_auto"
                            )
                            print(f"策略3成功 - 音频文件上传成功: {audio_file.name}")
                            upload_success = True
                            
                        except Exception as upload_error3:
                            print(f"策略3失败: {upload_error3}")
                            raise ValueError(f"所有音频上传策略都失败了: 策略1({upload_error1}), 策略2({upload_error2}), 策略3({upload_error3})")
                else:
                    # 非WebM格式，尝试自动检测
                    try:
                        print("策略2 - 尝试自动检测MIME类型...")
                        audio_file = genai.upload_file(
                            upload_filename,
                            display_name=f"interview_audio_{audio_format}"
                        )
                        print(f"策略2成功 - 音频文件上传成功: {audio_file.name}")
                        upload_success = True
                        
                    except Exception as upload_error2:
                        print(f"策略2失败: {upload_error2}")
                        raise ValueError(f"音频文件上传失败: 策略1({upload_error1}), 策略2({upload_error2})")
            
            if not upload_success or not audio_file:
                raise ValueError("音频文件上传失败")
            
            # 发送上传成功通知
            try:
                socketio.emit('processing_status', {
                    'stage': 'audio_processing',
                    'message': '音频文件上传成功，正在处理...',
                    'progress': 30
                }, to=session_id)
            except Exception as emit_error:
                print(f"发送状态通知失败: {emit_error}")
            
            # 等待文件处理完成，优化等待策略
            print(f"文件初始状态: {audio_file.state.name}")
            
            # 如果文件已经是ACTIVE状态，直接跳过等待
            if audio_file.state.name == "ACTIVE":
                print("音频文件已经准备就绪，直接开始处理")
            else:
                max_wait_time = 30  # 减少最大等待时间到30秒
                wait_time = 0
                check_interval = 1  # 减少检查间隔到1秒
                
                while audio_file.state.name == "PROCESSING" and wait_time < max_wait_time:
                    print(f"等待Gemini处理音频文件... ({wait_time}s)")
                    await asyncio.sleep(check_interval)
                    wait_time += check_interval
                    try:
                        audio_file = genai.get_file(audio_file.name)
                        print(f"当前文件状态: {audio_file.state.name}")
                        
                        # 如果状态已经变为ACTIVE，立即跳出循环
                        if audio_file.state.name == "ACTIVE":
                            print("音频文件处理完成，立即开始生成回答")
                            break
                            
                    except Exception as status_error:
                        print(f"获取文件状态时出错: {status_error}")
                        break
                
                # 检查最终状态
                print(f"文件最终状态: {audio_file.state.name}")
                
                if wait_time >= max_wait_time and audio_file.state.name == "PROCESSING":
                    print("音频文件处理超时，但尝试继续处理...")
                    # 不立即失败，尝试继续处理
            
            # 检查文件最终状态
            if audio_file.state.name == "FAILED":
                # 尝试获取更详细的错误信息
                error_details = getattr(audio_file, 'error', '未知错误')
                print(f"音频文件处理失败: {error_details}")
                
                # 如果是视频处理错误，尝试使用文本输入作为fallback
                if 'video_utils' in str(error_details) or 'video' in str(error_details).lower():
                    print("检测到视频处理错误，可能是音频格式问题")
                    # 发送特殊错误信息，提示用户使用文本输入
                    try:
                        socketio.emit('audio_processing_failed', {
                            'message': '音频处理遇到技术问题，请尝试以下解决方案：',
                            'suggestions': [
                                '1. 重新录制音频（确保录制质量良好）',
                                '2. 或者直接在下方输入您的问题文本',
                                '3. 检查麦克风设置和网络连接'
                            ],
                            'show_text_input': True
                        }, to=session_id)
                        print("已发送音频处理失败通知和文本输入建议")
                    except Exception as emit_error:
                        print(f"发送音频失败通知时出错: {emit_error}")
                    
                    # 清理文件并返回
                    try:
                        genai.delete_file(audio_file.name)
                    except:
                        pass
                    
                    if voice_record:
                        voice_record.status = 'failed'
                        voice_record.answer = "音频处理失败，建议使用文本输入"
                        db.session.commit()
                    
                    return None
                else:
                    raise ValueError(f"Gemini音频文件处理失败: {error_details}")
            
            if audio_file.state.name != "ACTIVE":
                raise ValueError(f"音频文件状态异常: {audio_file.state.name}")
            
            print("音频文件处理完成，开始生成回答...")
            
            # 发送AI生成开始通知
            try:
                socketio.emit('processing_status', {
                    'stage': 'ai_generation',
                    'message': '音频处理完成，正在生成回答...',
                    'progress': 60
                }, to=session_id)
            except Exception as emit_error:
                print(f"发送状态通知失败: {emit_error}")
            
            # 生成系统提示词 - 添加语音识别质量检测
            system_prompt = self.generate_system_prompt(user_info)
            
            # 构建完整的提示词
            full_prompt = f"""{system_prompt}

请根据上面的音频内容，生成一个专业的面试回答。

**重要提醒**：
- 只有在能够清晰理解音频中的具体问题时，才生成专业的面试回答
- 回答应该直接回应音频中提到的问题，结合我的背景信息和经验
- 使用Markdown格式，包含适当的标题、列表等

请开始分析音频并回答："""

            # 生成流式响应 - 使用多种模型尝试
            print("开始生成AI回答...")
            response = None
            generation_success = False
            
            # 尝试不同的模型
            models_to_try = ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-1.5-pro"]
            
            for model_name in models_to_try:
                try:
                    print(f"尝试使用模型: {model_name}")
                    response = genai.GenerativeModel(model_name).generate_content(
                        [full_prompt, audio_file],
                        stream=True,
                        safety_settings=self.safety_settings
                    )
                    print(f"模型 {model_name} 生成成功")
                    generation_success = True
                    break
                except Exception as model_error:
                    print(f"模型 {model_name} 生成失败: {model_error}")
                    continue
            
            if not generation_success or not response:
                raise ValueError("所有AI模型都无法生成回答")
            
            full_response = ""
            chunk_count = 0
            recognized_question = "语音问题"  # 这里可以扩展为实际的语音识别
            
            # 更新语音记录的问题
            if voice_record:
                voice_record.question = recognized_question
                db.session.commit()
            
            try:
                for chunk in response:
                    chunk_count += 1
                    print(f"处理第 {chunk_count} 个响应块...")
                    
                    try:
                        if hasattr(chunk, 'text') and chunk.text:
                            chunk_text = chunk.text
                            full_response += chunk_text
                            print(f"发送响应块: {chunk_text[:50]}...")
                            # 通过WebSocket发送流式数据到特定客户端
                            try:
                                socketio.emit('response_chunk', {'text': chunk_text}, to=session_id)
                                print(f"成功发送响应块到session {session_id}")
                            except Exception as emit_error:
                                print(f"发送响应块失败: {emit_error}")
                        elif hasattr(chunk, 'candidates') and chunk.candidates:
                            for candidate in chunk.candidates:
                                if hasattr(candidate, 'content') and candidate.content:
                                    if hasattr(candidate.content, 'parts') and candidate.content.parts:
                                        for part in candidate.content.parts:
                                            if hasattr(part, 'text') and part.text:
                                                chunk_text = part.text
                                                full_response += chunk_text
                                                print(f"发送响应块(候选): {chunk_text[:50]}...")
                                                try:
                                                    socketio.emit('response_chunk', {'text': chunk_text}, to=session_id)
                                                    print(f"成功发送响应块(候选)到session {session_id}")
                                                except Exception as emit_error:
                                                    print(f"发送响应块(候选)失败: {emit_error}")
                    except Exception as chunk_error:
                        print(f"处理响应块时出错: {chunk_error}")
                        continue
                        
            except Exception as stream_error:
                print(f"流式响应处理出错: {stream_error}")
                if not full_response:
                    raise ValueError(f"无法获取AI响应: {stream_error}")
            
            # 清理上传的文件
            try:
                genai.delete_file(audio_file.name)
                print(f"已删除Gemini上的音频文件: {audio_file.name}")
            except Exception as delete_error:
                print(f"删除Gemini文件失败: {delete_error}")
            
            print(f"AI回答生成完成，总共 {chunk_count} 个块，响应长度: {len(full_response)}")
            
            # 检查是否是"听不清楚"的回答
            if "听不清楚" in full_response or "听不懂" in full_response:
                print("AI反馈音频不清楚，建议用户重新录制或使用文字输入")
                try:
                    socketio.emit('audio_quality_poor', {
                        'message': '音频识别质量不佳',
                        'suggestions': [
                            '1. 请在安静环境中重新录制',
                            '2. 说话清晰，语速适中',
                            '3. 确保麦克风距离适当',
                            '4. 或者直接使用文字输入功能'
                        ],
                        'show_text_input': True
                    }, to=session_id)
                except Exception as emit_error:
                    print(f"发送音频质量提醒失败: {emit_error}")
            
            # 更新语音记录
            if voice_record:
                voice_record.answer = full_response
                voice_record.status = 'completed'
                voice_record.completed_at = datetime.utcnow()
                db.session.commit()
                print(f"语音记录 {voice_record.id} 已完成并保存")
            
            # 发送完成信号
            try:
                socketio.emit('response_complete', {
                    'full_text': full_response,
                    'record_id': voice_record.id if voice_record else None
                }, to=session_id)
                print(f"成功发送完成信号到session {session_id}")
            except Exception as emit_error:
                print(f"发送完成信号失败: {emit_error}")
                
            # 重置当前记录ID
            if self.current_record_id == (voice_record.id if voice_record else None):
                self.current_record_id = None
                self.current_session_id = None
                
            return full_response
            
        except Exception as e:
            error_msg = f"处理音频时出错: {str(e)}"
            print(f"错误详情: {error_msg}")
            
            # 更新语音记录状态为失败
            if voice_record:
                voice_record.status = 'failed'
                voice_record.answer = f"生成失败: {error_msg}"
                db.session.commit()
            
            try:
                socketio.emit('error', {'message': error_msg}, to=session_id)
                print(f"成功发送错误信号到session {session_id}")
            except Exception as emit_error:
                print(f"发送错误信号失败: {emit_error}")
                
            # 重置当前记录ID
            self.current_record_id = None
            self.current_session_id = None
            return None
            
        finally:
            # 清理临时文件
            if temp_filename and os.path.exists(temp_filename):
                try:
                    os.unlink(temp_filename)
                    print(f"原始临时文件已删除: {temp_filename}")
                except Exception as cleanup_error:
                    print(f"清理原始临时文件失败: {cleanup_error}")
            
            # 清理转换后的临时文件
            if 'converted_filename' in locals() and temp_filename and os.path.exists(temp_filename):
                try:
                    os.unlink(temp_filename)
                    print(f"转换后临时文件已删除: {temp_filename}")
                except Exception as cleanup_error:
                    print(f"清理转换后临时文件失败: {cleanup_error}")

    async def process_text_question_and_generate_response(self, question, user_info, session_id, user_id=None, config_id=None):
        """处理文本问题并生成回答"""
        voice_record = None
        
        try:
            print(f"=== 开始处理文本问题 ===")
            print(f"问题: {question}")
            print(f"Session ID: {session_id}")
            print(f"User ID: {user_id}")
            print(f"Config ID: {config_id}")
            
            # 如果有正在处理的记录，标记为中断
            if self.current_record_id and user_id:
                try:
                    old_record = VoiceRecord.query.get(self.current_record_id)
                    if old_record and old_record.status == 'generating':
                        old_record.status = 'interrupted'
                        db.session.commit()
                        print(f"标记之前的记录 {self.current_record_id} 为中断状态")
                        
                        # 发送通知给前端
                        socketio.emit('voice_record_interrupted', {
                            'record_id': self.current_record_id,
                            'message': '之前的语音生成已中断，新内容已保存到历史记录'
                        }, to=self.current_session_id)
                        
                except Exception as e:
                    print(f"处理之前记录时出错: {e}")
            
            # 创建新的语音记录
            if user_id:
                voice_record = VoiceRecord(
                    user_id=user_id,
                    config_id=config_id,
                    question=question,
                    status='generating'
                )
                db.session.add(voice_record)
                db.session.commit()
                
                self.current_record_id = voice_record.id
                self.current_session_id = session_id
                print(f"创建新的语音记录 ID: {voice_record.id}")
            
            # 生成系统提示词
            system_prompt = self.generate_system_prompt(user_info)
            
            # 构建完整的提示词
            full_prompt = f"""{system_prompt}

面试官问题：{question}

请根据上面的问题，生成一个专业的面试回答。回答应该：
- 直接回应问题要点
- 结合我的背景信息和经验
- 展示相关的技能和能力
- 使用Markdown格式，包含适当的标题、列表等

请开始回答："""

            # 生成流式响应
            print("开始生成AI回答...")
            response = None
            generation_success = False
            
            # 尝试不同的模型
            models_to_try = ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-1.5-pro"]
            
            for model_name in models_to_try:
                try:
                    print(f"尝试使用模型: {model_name}")
                    response = genai.GenerativeModel(model_name).generate_content(
                        full_prompt,
                        stream=True,  # 启用流式输出
                        safety_settings=self.safety_settings
                    )
                    print(f"模型 {model_name} 生成成功")
                    generation_success = True
                    break
                except Exception as model_error:
                    print(f"模型 {model_name} 生成失败: {model_error}")
                    continue
            
            if not generation_success or not response:
                raise ValueError("所有AI模型都无法生成回答")
            
            full_response = ""
            chunk_count = 0
            
            try:
                for chunk in response:
                    chunk_count += 1
                    print(f"处理第 {chunk_count} 个响应块...")
                    
                    try:
                        if hasattr(chunk, 'text') and chunk.text:
                            chunk_text = chunk.text
                            full_response += chunk_text
                            print(f"发送响应块: {chunk_text[:50]}...")
                            # 通过WebSocket发送流式数据到特定客户端
                            try:
                                socketio.emit('response_chunk', {'text': chunk_text}, to=session_id)
                                print(f"成功发送响应块到session {session_id}")
                            except Exception as emit_error:
                                print(f"发送响应块失败: {emit_error}")
                        elif hasattr(chunk, 'candidates') and chunk.candidates:
                            for candidate in chunk.candidates:
                                if hasattr(candidate, 'content') and candidate.content:
                                    if hasattr(candidate.content, 'parts') and candidate.content.parts:
                                        for part in candidate.content.parts:
                                            if hasattr(part, 'text') and part.text:
                                                chunk_text = part.text
                                                full_response += chunk_text
                                                print(f"发送响应块(候选): {chunk_text[:50]}...")
                                                try:
                                                    socketio.emit('response_chunk', {'text': chunk_text}, to=session_id)
                                                    print(f"成功发送响应块(候选)到session {session_id}")
                                                except Exception as emit_error:
                                                    print(f"发送响应块(候选)失败: {emit_error}")
                    except Exception as chunk_error:
                        print(f"处理响应块时出错: {chunk_error}")
                        continue
                        
            except Exception as stream_error:
                print(f"流式响应处理出错: {stream_error}")
                if not full_response:
                    raise ValueError(f"无法获取AI响应: {stream_error}")
            
            print(f"AI回答生成完成，总共 {chunk_count} 个块，响应长度: {len(full_response)}")
            
            # 更新语音记录
            if voice_record:
                voice_record.answer = full_response
                voice_record.status = 'completed'
                voice_record.completed_at = datetime.utcnow()
                db.session.commit()
                print(f"语音记录 {voice_record.id} 已完成并保存")
            
            # 发送完成信号
            try:
                socketio.emit('response_complete', {
                    'full_text': full_response,
                    'record_id': voice_record.id if voice_record else None
                }, to=session_id)
                print(f"成功发送完成信号到session {session_id}")
            except Exception as emit_error:
                print(f"发送完成信号失败: {emit_error}")
                
            # 重置当前记录ID
            if self.current_record_id == (voice_record.id if voice_record else None):
                self.current_record_id = None
                self.current_session_id = None
                
            return full_response
            
        except Exception as e:
            error_msg = f"处理文本问题时出错: {str(e)}"
            print(f"错误详情: {error_msg}")
            
            # 更新语音记录状态为失败
            if voice_record:
                voice_record.status = 'failed'
                voice_record.answer = f"生成失败: {error_msg}"
                db.session.commit()
            
            try:
                socketio.emit('error', {'message': error_msg}, to=session_id)
                print(f"成功发送错误信号到session {session_id}")
            except Exception as emit_error:
                print(f"发送错误信号失败: {emit_error}")
                
            # 重置当前记录ID
            self.current_record_id = None
            self.current_session_id = None
            return None

    async def generate_interview_transcript(self, user_info, session_id, user_id=None, config_id=None):
        """生成面试逐字稿"""
        transcript_record = None
        
        try:
            print(f"=== 开始生成面试逐字稿 ===")
            print(f"Session ID: {session_id}")
            print(f"User ID: {user_id}")
            print(f"Config ID: {config_id}")
            print(f"用户信息: {user_info}")
            
            # 发送处理开始通知
            try:
                socketio.emit('transcript_processing_start', {
                    'message': '正在生成面试逐字稿...'
                }, to=session_id)
            except Exception as emit_error:
                print(f"发送状态通知失败: {emit_error}")
            
            # 创建新的逐字稿记录
            if user_id:
                config = InterviewConfig.query.get(config_id) if config_id else None
                title = f"{config.company} {config.job_title} 面试逐字稿" if config else "面试逐字稿"
                
                transcript_record = InterviewTranscript(
                    user_id=user_id,
                    config_id=config_id,
                    title=title,
                    content="",
                    status='generating'
                )
                db.session.add(transcript_record)
                db.session.commit()
                print(f"创建新的逐字稿记录 ID: {transcript_record.id}")
            
            # 构建逐字稿生成提示词
            transcript_prompt = f"""我目前将要面试{user_info.get('company', '')}的{user_info.get('job_title', '')}岗位。

请你基于我的个人简历:
```
{user_info.get('resume', '')}

详细工作经历:
{user_info.get('detailed_experience', '')}
```

和岗位JD:
```
{user_info.get('job_description', '')}
```

帮我生成一份长篇的面试逐字稿，注意:
1. 突出个人对{user_info.get('job_title', '')}技术及业务的理解，突出个人具备岗位所要求的技术及工作流；
2. 体现个人对未来的职业规划非常清晰和坚定，个人职业规划坚定锚定{user_info.get('job_title', '')}方向；
3. 体现个人沟通能力、学习能力、技术或业务能力；
4. 在讲解具体工作或项目经历时的逻辑需要结合岗位jd各点描述自己在实际工作中发现了哪些问题，做了哪些工作(与岗位贴合)，怎么解决，结果如何？但不要在逐字稿中说我契合岗位jd中的哪一点；
5. 口语化表述；
6. 直接生成逐字稿内容不做任何解释。

请生成详细的面试逐字稿："""

            # 生成流式响应
            print("开始生成逐字稿...")
            response = None
            generation_success = False
            
            # 尝试不同的模型
            models_to_try = ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-1.5-pro"]
            
            for model_name in models_to_try:
                try:
                    print(f"尝试使用模型: {model_name}")
                    response = genai.GenerativeModel(model_name).generate_content(
                        transcript_prompt,
                        stream=True,  # 启用流式输出
                        safety_settings=self.safety_settings
                    )
                    print(f"模型 {model_name} 生成成功")
                    generation_success = True
                    break
                except Exception as model_error:
                    print(f"模型 {model_name} 生成失败: {model_error}")
                    continue
            
            if not generation_success or not response:
                raise ValueError("所有AI模型都无法生成逐字稿")
            
            full_transcript = ""
            chunk_count = 0
            
            try:
                for chunk in response:
                    chunk_count += 1
                    print(f"处理第 {chunk_count} 个逐字稿响应块...")
                    
                    try:
                        if hasattr(chunk, 'text') and chunk.text:
                            chunk_text = chunk.text
                            full_transcript += chunk_text
                            print(f"发送逐字稿响应块: {chunk_text[:50]}...")
                            # 通过WebSocket发送流式数据到特定客户端
                            try:
                                socketio.emit('transcript_response_chunk', {'text': chunk_text}, to=session_id)
                                print(f"成功发送逐字稿响应块到session {session_id}")
                            except Exception as emit_error:
                                print(f"发送逐字稿响应块失败: {emit_error}")
                        elif hasattr(chunk, 'candidates') and chunk.candidates:
                            for candidate in chunk.candidates:
                                if hasattr(candidate, 'content') and candidate.content:
                                    if hasattr(candidate.content, 'parts') and candidate.content.parts:
                                        for part in candidate.content.parts:
                                            if hasattr(part, 'text') and part.text:
                                                chunk_text = part.text
                                                full_transcript += chunk_text
                                                print(f"发送逐字稿响应块(候选): {chunk_text[:50]}...")
                                                try:
                                                    socketio.emit('transcript_response_chunk', {'text': chunk_text}, to=session_id)
                                                    print(f"成功发送逐字稿响应块(候选)到session {session_id}")
                                                except Exception as emit_error:
                                                    print(f"发送逐字稿响应块(候选)失败: {emit_error}")
                    except Exception as chunk_error:
                        print(f"处理逐字稿响应块时出错: {chunk_error}")
                        continue
                        
            except Exception as stream_error:
                print(f"逐字稿流式响应处理出错: {stream_error}")
                if not full_transcript:
                    raise ValueError(f"无法获取逐字稿AI响应: {stream_error}")
            
            print(f"逐字稿生成完成，总共 {chunk_count} 个块，响应长度: {len(full_transcript)}")
            
            # 更新逐字稿记录
            if transcript_record:
                transcript_record.content = full_transcript
                transcript_record.status = 'completed'
                transcript_record.completed_at = datetime.utcnow()
                db.session.commit()
                print(f"逐字稿记录 {transcript_record.id} 已完成并保存")
            
            # 发送完成信号
            try:
                socketio.emit('transcript_response_complete', {
                    'full_text': full_transcript,
                    'transcript_id': transcript_record.id if transcript_record else None,
                    'title': transcript_record.title if transcript_record else None
                }, to=session_id)
                print(f"成功发送逐字稿完成信号到session {session_id}")
            except Exception as emit_error:
                print(f"发送逐字稿完成信号失败: {emit_error}")
                
            return full_transcript
            
        except Exception as e:
            error_msg = f"生成逐字稿时出错: {str(e)}"
            print(f"错误详情: {error_msg}")
            
            # 更新逐字稿记录状态为失败
            if transcript_record:
                transcript_record.status = 'failed'
                transcript_record.content = f"生成失败: {error_msg}"
                db.session.commit()
            
            try:
                socketio.emit('transcript_error', {'message': error_msg}, to=session_id)
                print(f"成功发送逐字稿错误信号到session {session_id}")
            except Exception as emit_error:
                print(f"发送逐字稿错误信号失败: {emit_error}")
                
            return None


# 全局助手实例
assistant = InterviewAssistant()


# 路由和API接口
@app.route('/')
def index():
    """主页面"""
    if not current_user.is_authenticated:
        return redirect(url_for('login'))
    return render_template('index.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    """登录页面"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error='用户名或密码错误')
    
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    """注册页面"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        # 验证输入
        if not username or not password:
            return render_template('register.html', error='用户名和密码不能为空')
        
        if password != confirm_password:
            return render_template('register.html', error='两次输入的密码不一致')
        
        # 检查用户名是否已存在
        if User.query.filter_by(username=username).first():
            return render_template('register.html', error='用户名已存在')
        
        # 创建新用户
        user = User(username=username)
        user.set_password(password)
        
        try:
            db.session.add(user)
            db.session.commit()
            login_user(user)
            return redirect(url_for('index'))
        except Exception as e:
            db.session.rollback()
            return render_template('register.html', error='注册失败，请重试')
    
    return render_template('register.html')


@app.route('/logout')
@login_required
def logout():
    """退出登录"""
    logout_user()
    return redirect(url_for('login'))


# API接口
@app.route('/api/user/profile')
@login_required
def get_user_profile():
    """获取用户信息"""
    return jsonify(current_user.to_dict())


@app.route('/api/configs')
@login_required
def get_configs():
    """获取用户的面试配置列表"""
    configs = InterviewConfig.query.filter_by(user_id=current_user.id).order_by(InterviewConfig.updated_at.desc()).all()
    return jsonify([config.to_dict() for config in configs])


@app.route('/api/configs', methods=['POST'])
@login_required
def create_config():
    """创建新的面试配置"""
    try:
        data = request.json
        
        # 验证必需字段
        required_fields = ['name', 'candidate_name', 'position', 'company', 'job_title']
        for field in required_fields:
            if not data.get(field):
                return jsonify({'error': f'{field} 是必需的'}), 400
        
        config = InterviewConfig(
            user_id=current_user.id,
            name=data['name'],
            candidate_name=data['candidate_name'],
            position=data['position'],
            company=data['company'],
            job_title=data['job_title'],
            resume=data.get('resume', ''),
            detailed_experience=data.get('detailed_experience', ''),
            job_description=data.get('job_description', '')
        )
        
        db.session.add(config)
        db.session.commit()
        
        return jsonify(config.to_dict()), 201
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': '创建配置失败'}), 500


@app.route('/api/configs/<int:config_id>')
@login_required
def get_config(config_id):
    """获取特定面试配置"""
    config = InterviewConfig.query.filter_by(id=config_id, user_id=current_user.id).first()
    if not config:
        return jsonify({'error': '配置不存在'}), 404
    return jsonify(config.to_dict())


@app.route('/api/configs/<int:config_id>', methods=['PUT'])
@login_required
def update_config(config_id):
    """更新面试配置"""
    try:
        config = InterviewConfig.query.filter_by(id=config_id, user_id=current_user.id).first()
        if not config:
            return jsonify({'error': '配置不存在'}), 404
        
        data = request.json
        
        # 更新字段
        if 'name' in data:
            config.name = data['name']
        if 'candidate_name' in data:
            config.candidate_name = data['candidate_name']
        if 'position' in data:
            config.position = data['position']
        if 'company' in data:
            config.company = data['company']
        if 'job_title' in data:
            config.job_title = data['job_title']
        if 'resume' in data:
            config.resume = data['resume']
        if 'detailed_experience' in data:
            config.detailed_experience = data['detailed_experience']
        if 'job_description' in data:
            config.job_description = data['job_description']
        
        config.updated_at = datetime.utcnow()
        
        db.session.commit()
        
        return jsonify(config.to_dict())
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': '更新配置失败'}), 500


@app.route('/api/configs/<int:config_id>', methods=['DELETE'])
@login_required
def delete_config(config_id):
    """删除面试配置"""
    try:
        config = InterviewConfig.query.filter_by(id=config_id, user_id=current_user.id).first()
        if not config:
            return jsonify({'error': '配置不存在'}), 404
        
        db.session.delete(config)
        db.session.commit()
        
        return jsonify({'message': '配置已删除'})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': '删除配置失败'}), 500


@app.route('/api/config', methods=['POST'])
@login_required
def save_config():
    """保存用户配置（兼容旧接口）"""
    try:
        config = request.json
        # 这里可以保存到数据库或缓存
        return jsonify({'status': 'success', 'message': '配置保存成功'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})


@socketio.on('connect')
def handle_connect():
    """WebSocket连接处理"""
    print('客户端已连接')
    emit('connected', {'message': '连接成功'})


@socketio.on('disconnect')
def handle_disconnect():
    """WebSocket断开连接处理"""
    print('客户端已断开连接')


@socketio.on('process_audio')
def handle_process_audio(data):
    """处理音频数据"""
    try:
        print(f"收到音频处理请求，数据键: {list(data.keys()) if data else 'None'}")
        
        audio_data = data.get('audio')
        user_info = data.get('userInfo', {})
        config_id = data.get('configId')
        
        print(f"音频数据长度: {len(audio_data) if audio_data else 0}")
        print(f"配置ID: {config_id}")
        print(f"用户信息: {user_info.get('name', 'Unknown')}")
        
        # 检查音频数据
        if not audio_data:
            print("错误：未收到音频数据")
            emit('error', {'message': '未收到音频数据'})
            return
        
        # 检查音频数据大小
        audio_size_mb = len(audio_data) / (1024 * 1024)
        print(f"音频数据大小: {audio_size_mb:.2f} MB")
        
        if audio_size_mb > 9:
            print(f"错误：音频文件过大 ({audio_size_mb:.2f} MB)")
            emit('error', {'message': f'音频文件过大 ({audio_size_mb:.2f} MB)，请缩短录音时间'})
            return
        
        if not audio_data.startswith('data:audio'):
            print(f"错误：音频数据格式无效，前缀: {audio_data[:50] if audio_data else 'None'}")
            emit('error', {'message': '音频数据格式无效'})
            return
        
        # 发送处理开始信号
        print("发送处理开始信号")
        emit('processing_start', {'message': '正在处理您的问题...'})
        
        # 获取当前session ID和用户ID
        session_id = request.sid
        user_id = current_user.id if current_user.is_authenticated else None
        print(f"处理音频请求，session ID: {session_id}, user ID: {user_id}")
        
        # 直接在主线程中处理音频
        try:
            print("开始创建事件循环处理音频...")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(
                assistant.process_audio_and_generate_response(
                    audio_data, user_info, session_id, user_id, config_id
                )
            )
            print(f"音频处理完成，结果长度: {len(result) if result else 0}")
        except Exception as loop_error:
            print(f"事件循环处理异常: {loop_error}")
            import traceback
            print(f"异常堆栈: {traceback.format_exc()}")
            emit('error', {'message': f'音频处理异常: {str(loop_error)}'})
        finally:
            try:
                loop.close()
                print("事件循环已关闭")
            except Exception as close_error:
                print(f"关闭事件循环时出错: {close_error}")
        
    except Exception as e:
        print(f"处理音频请求异常: {str(e)}")
        import traceback
        print(f"异常堆栈: {traceback.format_exc()}")
        emit('error', {'message': f'处理请求时出错: {str(e)}'})


@socketio.on('process_text_question')
def handle_process_text_question(data):
    """处理文本问题"""
    try:
        question = data.get('question', '').strip()
        user_info = data.get('userInfo', {})
        config_id = data.get('configId')
        
        if not question:
            emit('error', {'message': '问题内容不能为空'})
            return
        
        # 发送处理开始信号
        emit('processing_start', {'message': '正在生成回答...'})
        
        # 获取当前session ID和用户ID
        session_id = request.sid
        user_id = current_user.id if current_user.is_authenticated else None
        print(f"处理文本问题，session ID: {session_id}, user ID: {user_id}, 问题: {question}")
        
        # 直接在主线程中处理文本问题
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(
                assistant.process_text_question_and_generate_response(
                    question, user_info, session_id, user_id, config_id
                )
            )
        finally:
            loop.close()
        
    except Exception as e:
        print(f"处理文本问题异常: {str(e)}")
        emit('error', {'message': f'处理文本问题时出错: {str(e)}'})


@socketio.on('generate_transcript')
def handle_generate_transcript(data):
    """处理逐字稿生成请求"""
    try:
        print(f"收到逐字稿生成请求，数据键: {list(data.keys()) if data else 'None'}")
        
        user_info = data.get('userInfo', {})
        config_id = data.get('configId')
        
        print(f"配置ID: {config_id}")
        print(f"用户信息: {user_info.get('name', 'Unknown')}")
        
        # 检查配置ID
        if not config_id:
            print("错误：未提供配置ID")
            emit('transcript_error', {'message': '请先选择一个面试配置'})
            return
        
        # 发送处理开始信号
        print("发送逐字稿处理开始信号")
        emit('transcript_processing_start', {'message': '正在生成面试逐字稿...'})
        
        # 获取当前session ID和用户ID
        session_id = request.sid
        user_id = current_user.id if current_user.is_authenticated else None
        print(f"处理逐字稿请求，session ID: {session_id}, user ID: {user_id}")
        
        # 直接在主线程中处理逐字稿生成
        try:
            print("开始创建事件循环处理逐字稿...")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(
                assistant.generate_interview_transcript(
                    user_info, session_id, user_id, config_id
                )
            )
            print(f"逐字稿处理完成，结果长度: {len(result) if result else 0}")
        except Exception as loop_error:
            print(f"事件循环处理异常: {loop_error}")
            import traceback
            print(f"异常堆栈: {traceback.format_exc()}")
            emit('transcript_error', {'message': f'逐字稿生成异常: {str(loop_error)}'})
        finally:
            try:
                loop.close()
                print("事件循环已关闭")
            except Exception as close_error:
                print(f"关闭事件循环时出错: {close_error}")
        
    except Exception as e:
        print(f"处理逐字稿请求异常: {str(e)}")
        import traceback
        print(f"异常堆栈: {traceback.format_exc()}")
        emit('transcript_error', {'message': f'处理逐字稿请求时出错: {str(e)}'})


def init_db():
    """初始化数据库"""
    with app.app_context():
        db.create_all()
        print("数据库初始化完成")


# API路由 - 语音记录管理
@app.route('/api/voice-records', methods=['GET'])
@login_required
def get_voice_records():
    """获取用户的语音记录列表"""
    try:
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 10, type=int)
        unread_only = request.args.get('unread_only', 'false').lower() == 'true'
        
        query = VoiceRecord.query.filter_by(user_id=current_user.id)
        
        if unread_only:
            query = query.filter_by(is_read=False)
            
        records = query.order_by(VoiceRecord.created_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )
        
        record_list = []
        for record in records.items:
            try:
                # 安全地获取配置名称
                config_name = None
                if record.config_id and record.config:
                    config_name = record.config.name
                
                record_data = {
                    'id': record.id,
                    'question': record.question or '语音问题',
                    'answer': record.answer or '',
                    'status': record.status,
                    'created_at': record.created_at.isoformat(),
                    'completed_at': record.completed_at.isoformat() if record.completed_at else None,
                    'is_read': record.is_read,
                    'config_name': config_name
                }
                record_list.append(record_data)
                
            except Exception as record_error:
                print(f"处理记录 {record.id} 时出错: {record_error}")
                continue
        
        return jsonify({
            'records': record_list,
            'pagination': {
                'page': records.page,
                'pages': records.pages,
                'per_page': records.per_page,
                'total': records.total,
                'has_next': records.has_next,
                'has_prev': records.has_prev
            }
        })
        
    except Exception as e:
        print(f"获取语音记录失败: {e}")
        return jsonify({'error': '获取记录失败'}), 500


@app.route('/api/voice-records/<int:record_id>')
@login_required
def get_voice_record(record_id):
    """获取特定语音记录"""
    try:
        record = VoiceRecord.query.filter_by(id=record_id, user_id=current_user.id).first()
        if not record:
            return jsonify({'error': '记录不存在'}), 404
        
        # 安全地获取配置名称
        config_name = None
        if record.config_id and record.config:
            config_name = record.config.name
        
        record_data = {
            'id': record.id,
            'question': record.question or '语音问题',
            'answer': record.answer or '',
            'status': record.status,
            'created_at': record.created_at.isoformat(),
            'completed_at': record.completed_at.isoformat() if record.completed_at else None,
            'is_read': record.is_read,
            'config_name': config_name
        }
        
        return jsonify(record_data)
        
    except Exception as e:
        print(f"获取语音记录详情失败: {e}")
        return jsonify({'error': '获取记录详情失败'}), 500


@app.route('/api/voice-records/<int:record_id>/mark-read', methods=['POST'])
@login_required
def mark_voice_record_read(record_id):
    """标记语音记录为已读"""
    try:
        record = VoiceRecord.query.filter_by(id=record_id, user_id=current_user.id).first()
        if not record:
            return jsonify({'error': '记录不存在'}), 404
        
        record.is_read = True
        db.session.commit()
        
        return jsonify({'message': '已标记为已读'})
        
    except Exception as e:
        print(f"标记已读失败: {e}")
        db.session.rollback()
        return jsonify({'error': '标记已读失败'}), 500


@app.route('/api/voice-records/unread-count')
@login_required
def get_unread_count():
    """获取未读语音记录数量"""
    try:
        count = VoiceRecord.query.filter_by(user_id=current_user.id, is_read=False).count()
        return jsonify({'count': count})
    except Exception as e:
        print(f"获取未读数量失败: {e}")
        return jsonify({'error': '获取未读数量失败'}), 500


# API路由 - 逐字稿管理
@app.route('/api/transcripts', methods=['GET'])
@login_required
def get_transcripts():
    """获取用户的逐字稿列表"""
    try:
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 10, type=int)
        
        query = InterviewTranscript.query.filter_by(user_id=current_user.id)
        
        transcripts = query.order_by(InterviewTranscript.created_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )
        
        transcript_list = []
        for transcript in transcripts.items:
            try:
                transcript_data = transcript.to_dict()
                transcript_list.append(transcript_data)
                
            except Exception as transcript_error:
                print(f"处理逐字稿 {transcript.id} 时出错: {transcript_error}")
                continue
        
        return jsonify({
            'transcripts': transcript_list,
            'pagination': {
                'page': transcripts.page,
                'pages': transcripts.pages,
                'per_page': transcripts.per_page,
                'total': transcripts.total,
                'has_next': transcripts.has_next,
                'has_prev': transcripts.has_prev
            }
        })
        
    except Exception as e:
        print(f"获取逐字稿列表失败: {e}")
        return jsonify({'error': '获取逐字稿列表失败'}), 500


@app.route('/api/transcripts/<int:transcript_id>')
@login_required
def get_transcript(transcript_id):
    """获取特定逐字稿"""
    try:
        transcript = InterviewTranscript.query.filter_by(id=transcript_id, user_id=current_user.id).first()
        if not transcript:
            return jsonify({'error': '逐字稿不存在'}), 404
        
        return jsonify(transcript.to_dict())
        
    except Exception as e:
        print(f"获取逐字稿详情失败: {e}")
        return jsonify({'error': '获取逐字稿详情失败'}), 500


@app.route('/api/transcripts/<int:transcript_id>', methods=['DELETE'])
@login_required
def delete_transcript(transcript_id):
    """删除逐字稿"""
    try:
        transcript = InterviewTranscript.query.filter_by(id=transcript_id, user_id=current_user.id).first()
        if not transcript:
            return jsonify({'error': '逐字稿不存在'}), 404
        
        db.session.delete(transcript)
        db.session.commit()
        
        return jsonify({'message': '逐字稿已删除'})
        
    except Exception as e:
        print(f"删除逐字稿失败: {e}")
        db.session.rollback()
        return jsonify({'error': '删除逐字稿失败'}), 500


if __name__ == '__main__':
    # 创建templates文件夹
    os.makedirs('templates', exist_ok=True)
    os.makedirs('static', exist_ok=True)
    
    # 初始化数据库
    init_db()
    
    print("=" * 60)
    print("🎯 面试助手Web应用")
    print("=" * 60)
    print("访问地址: http://localhost:5001")
    print("按 Ctrl+C 退出")
    print("-" * 60)
    
    socketio.run(app, host='0.0.0.0', port=5001, debug=True) 