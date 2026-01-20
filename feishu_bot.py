import json
import time
from datetime import datetime, timedelta
from io import BytesIO
from typing import Dict, List

from jinja2.runtime import new_context

from backend.feishu_message.model.chat_conversation import ChatConversation
from backend.feishu_message.model.chat_message import ChatMessage
from backend.feishu_message.model.chat_participant import ChatParticipant
from backend.public.init_engine import db_session
import uuid
import requests

from backend.public.minio_conn import MinioConnection


class FeishuBot:
    def __init__(self, app_id, app_secret):
        self.app_id = app_id
        self.app_secret = app_secret
        self.access_token = self._get_access_token()
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        self.minio_client = MinioConnection()


    def _get_access_token(self):
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        headers = {'Content-Type': 'application/json'}
        data = {
            "app_id": self.app_id,
            "app_secret": self.app_secret
        }
        response = requests.post(url, headers=headers, data=json.dumps(data))
        return response.json()["tenant_access_token"]

    # 查询机器人id信息
    def get_bot_id(self):
        url = "https://open.feishu.cn/open-apis/bot/v3/info"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        response = requests.get(url, headers=headers)
        bot_info = response.json()
        return bot_info["bot"]["open_id"]

    # 获取群组列表
    def get_chat_list(self):
        url = "https://open.feishu.cn/open-apis/im/v1/chats"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

        response = requests.get(url, headers=headers)

        return response.json()["data"]["items"]  # 返回群组列表

    # 根据名称获取群组
    @staticmethod
    def get_chat_by_name(chats: List[Dict], chat_name: str):
        for chat in chats:
            if chat.get("name") == chat_name:
                return chat
        return None

    def get_user_info(self, user_id: str) -> dict:
        response = None
        try:
            """根据 user_id 查询用户信息"""
            url = f"https://open.feishu.cn/open-apis/contact/v3/users/{user_id}"
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"
            }
            response = requests.get(url, headers=headers)
            if 'user' in json.loads(response.text)['data'].keys():
                return json.loads(response.text)['data']["user"]
            else:
                return json.loads(response.text)['data']["items"][0]
        except Exception as e:
            print(f"获取用户信息失败: {e}")
            print(response.text)
            return {"name": "未知"}

    # 获取群组成员
    def get_all_members(self, chat_id: str) -> List[Dict]:
        """获取指定群聊的所有成员"""
        all_members = []
        page_token = None
        has_more = True

        while has_more:
            url = f"https://open.feishu.cn/open-apis/im/v1/chats/{chat_id}/members"
            params = {
                "page_size": 100,  # 每次最多获取100个成员
                "member_id_type": "user_id"  # 返回用户ID格式
            }
            if page_token:
                params["page_token"] = page_token

            response = requests.get(url, headers=self.headers, params=params)
            data = response.json()

            if data.get("code") != 0:
                print(f"获取成员失败: {data.get('msg')}")
                break

            members = data.get("data", {}).get("items", [])
            all_members.extend(members)

            has_more = data.get("data", {}).get("has_more", False)
            page_token = data.get("data", {}).get("page_token")

            print(f"已获取 {len(all_members)} 位成员...")

        return all_members

    # 获取范围内的所有消息
    def get_all_messages(self, chat_id, days=30):
        """获取指定群组的所有历史消息"""
        end_time = int(datetime.now().timestamp())
        start_time = int((datetime.now() - timedelta(days=days)).timestamp())

        all_messages = []
        page_token = None
        has_more = True
        while has_more:
            params = {
                "container_id_type": "chat",
                "container_id": chat_id,
                "start_time": str(start_time),
                "end_time": str(end_time),
                "page_size": 50  # 每次最多获取50条
            }
            if page_token:
                params["page_token"] = page_token

            response = requests.get(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                headers=self.headers,
                params=params
            )

            data = response.json()
            if data.get("code") != 0:
                print(f"获取消息失败: {data.get('msg')}")
                break

            messages = data.get("data", {}).get("items", [])
            all_messages.extend(messages)

            has_more = data.get("data", {}).get("has_more", False)
            page_token = data.get("data", {}).get("page_token")

            # 打印进度
            print(f"已获取 {len(all_messages)} 条消息...")

            # 避免请求过于频繁
            time.sleep(0.5)

        return all_messages

    # 获取活跃成员
    def get_active_members(self, messages: List[Dict]) -> List[Dict]:
        """获取近期参与聊天的活跃成员"""
        # 1. 从消息中提取发言用户
        active_members = {}
        for msg in messages:
            sender = msg.get("sender", {})
            sender_id = sender.get("id", "")
            if sender_id and sender_id not in active_members:
                active_members[sender_id] = {
                    "user_id": sender_id,
                    "type": sender.get("sender_type", "user")
                }

        # 3. 过滤掉机器人消息
        return [m for m in active_members.values()]

    def get_file_download_url(self, message_id: str, file_key: str, filetype: str, file_name: str = None):
        try:
            """获取文件下载链接"""
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type={filetype}"
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"
            }
            bucket_name = "zleap-resources"
            response = requests.get(url, headers=headers)
            # 2. 创建内存缓冲区
            file_content = BytesIO()
            for chunk in response.iter_content(chunk_size=8192):
                file_content.write(chunk)
            file_content.seek(0)  # 重置指针位置
            
            if not self.minio_client.conn.bucket_exists(bucket_name):
                try:
                    self.minio_client.conn.make_bucket(bucket_name)
                    print(f"已创建MinIO存储桶: {bucket_name}")
                except Exception as e:
                    print(f"创建MinIO存储桶失败: {str(e)}")
                    return f"创建存储桶失败: {str(e)}"
            timestamp = int(time.time())
            if not file_name:
                content_type = response.headers.get('Content-Type').split('/')[-1]
                unique_filename = f"{timestamp}_{file_key}.{content_type}"
            else:
                unique_filename = f"{timestamp}_{file_name}"
            result = self.minio_client.put(
                bucket_name=bucket_name,
                object_name=unique_filename,
                data=file_content,
                length=file_content.getbuffer().nbytes,
            )
            if not result:
                return f"上传文件到MinIO失败"

            storage_path = f"{bucket_name}/{unique_filename}"
            return storage_path
        except Exception as e:
            print(f"获取文件下载链接失败: {e}")
            return ""

    # 保存会话数据
    def save_chat_conversation(self, conversation_data: List[Dict]):
        with db_session() as db:
            for cd in conversation_data:
                db.add(ChatConversation(**cd))

    # 保存消息数据
    def save_chat_message(self, uid: str, chat_name: str, message_data: List[Dict]):
        try:
            with db_session() as db:
                for md in message_data:
                    # 将时间戳转换为datetime对象
                    dt_object = datetime.fromtimestamp(int(md.get("create_time", 0)) / 1000)
                    msg_type = md['msg_type'].upper()
                    content = md['body']['content']
                    content_json = json.loads(content)
                    if not content_json:
                        continue
                    message = {
                        "conversation_id": uid,
                        "timestamp": dt_object,
                        "content": content_json['text'] if "text" in content_json.keys() else content,
                        "sender_id": md['sender']['id'],
                        "sender_name": self.get_user_info(md['sender']['id'])['name'],
                        "sender_title": "用户",
                        "extra_data": content_json,
                        "id": uuid.uuid4(),
                        "source_message_id": md["message_id"],
                        "type": md['msg_type'].upper(),
                        "sender_role": md['sender']['sender_type'].upper(),
                        "created_time": datetime.now(),
                        "updated_time": datetime.now(),
                        "sender_avatar": None,
                        "delete_time": None,
                        "source_type_id": "1c5162df-eace-4a71-9a97-322a568c22ed",
                        "del_flag": md['deleted'],
                        "priority": None,
                        "is_compressed": 0,
                    }
                    if msg_type == "IMAGE":
                        image_key = content_json['image_key']
                        new_content = {"type": md['msg_type'],
                                        "url": self.get_file_download_url(message['source_message_id'], image_key, md['msg_type']),
                                        "description": ""
                                       }
                        message['content'] = json.dumps(new_content)
                    elif msg_type == "FILE":
                        file_name = content_json['file_name']
                        file_key = content_json['file_key']
                        new_content = {"type": md['msg_type'],
                                       "url": self.get_file_download_url(message['source_message_id'],
                                                                         file_key, md['msg_type'], file_name),
                                       "description": ""
                                       }
                        message['content'] = json.dumps(new_content)
                    elif msg_type == "SYSTEM":
                        message['sender_role'] = "SYSTEM"
                    elif msg_type == "POST":
                        message['content'] = json.dumps(content_json['content'])
                    db.add(ChatMessage(**message))
        except Exception as e:
            print(f"保存消息数据失败: {e}")

    # 保存参与者数据
    def save_chat_participants(self, uid: str, members: List[Dict]):
        with db_session() as db:
            for member in members:
                pd = {
                    "conversation_id": uid,
                    "participant_id": member.get("user_id", ""),
                    "name": self.get_user_info(member.get("user_id", ""))['name'],
                    "id": uuid.uuid4(),
                    "role": member.get("type", "OTHER").upper(),
                    "created_time": datetime.now(),
                    "updated_time": datetime.now(),
                }
                db.add(ChatParticipant(**pd))

    def main(self):
        chat_list = self.get_chat_list()
        bot_id = self.get_bot_id()
        for cl in chat_list:
            uid = uuid.uuid4()
            conversation_data = []
            c_id = cl.get("chat_id", "")
            messages = self.get_all_messages(c_id)
            # 'ou_fbc59fb279a77312f334cc143f567c98'
            active_members = self.get_active_members(messages)
            # 将时间戳转换为datetime对象
            dt_object = datetime.fromtimestamp(int(messages[-1].get("create_time", 0))/ 1000)
            conversation_data.append({
                "source_config_id": "3a22df19-dd28-485d-9a41-97eb4700578b",
                "last_message_time": dt_object,
                "id": uid,
                "participants_count": len(active_members),
                "created_time": datetime.now(),
                "updated_time": datetime.now(),
                "messages_count": len(messages),
                "assistant_id": "85a243f3-c7b1-42c2-a136-35d87073e9c4",
                "title": cl.get("name", ""),
                "extra_data": None,
                "source_id": None
            })
            self.save_chat_conversation(conversation_data)
            self.save_chat_message(uid, cl.get("name", ""), messages)
            self.save_chat_participants(uid, active_members)
            break


if __name__ == "__main__":
    app_id = "cli_a81b7302f9ff900b"
    app_secret = "mmaQNtyVpWYZR3rv3ofejf8AKJvXTb1o"
    bot = FeishuBot(app_id, app_secret)
    bot.main()