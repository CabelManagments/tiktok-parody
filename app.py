import os, json, uuid
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, send_from_directory, url_for, session
from flask_socketio import SocketIO, emit, join_room
import re

app = Flask(__name__)
app.config['SECRET_KEY'] = 'supersecretkey123'
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv', 'webm'}

socketio = SocketIO(app, cors_allowed_origins="*")
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
DB_FILE = 'data.json'

def allowed_file(f): return '.' in f and f.rsplit('.',1)[1].lower() in ALLOWED_EXTENSIONS

def extract_hashtags(text):
    """Извлекает хэштеги из текста"""
    return re.findall(r'#\w+', text)

def load_data():
    if not os.path.exists(DB_FILE): return {"videos": [], "users": {}, "chats": {}, "streaks": {}}
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
            for k in ['videos','users','chats','streaks']:
                if k not in d: d[k] = {}
            if isinstance(d['videos'], dict): d['videos'] = []
            for v in d['videos']:
                if 'reposts' not in v: v['reposts'] = 0
                if 'reposted_by' not in v: v['reposted_by'] = []
                if 'views' not in v: v['views'] = 0
                if 'hashtags' not in v: v['hashtags'] = []
                if 'view_history' not in v: v['view_history'] = []
            for u in d['users']:
                if 'reposted_videos' not in d['users'][u]:
                    d['users'][u]['reposted_videos'] = []
                if 'watch_history' not in d['users'][u]:
                    d['users'][u]['watch_history'] = []
            return d
    except: return {"videos": [], "users": {}, "chats": {}, "streaks": {}}

def save_data(d): 
    with open(DB_FILE, 'w', encoding='utf-8') as f: 
        json.dump(d, f, indent=2, ensure_ascii=False)

def update_streak(user1, user2):
    data = load_data()
    streak_key = f"{min(user1, user2)}_{max(user1, user2)}"
    today = datetime.now().date().isoformat()
    
    if streak_key not in data['streaks']:
        data['streaks'][streak_key] = {'users': [user1, user2], 'count': 1, 'last_message_date': today}
        save_data(data)
        return 1
    
    streak = data['streaks'][streak_key]
    if streak.get('last_message_date') == today:
        return streak['count']
    
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    streak['count'] = streak['count'] + 1 if streak.get('last_message_date') == yesterday else 1
    streak['last_message_date'] = today
    save_data(data)
    
    socketio.emit('streak_update', {'with_user': user2, 'streak': streak['count']}, room=user1)
    socketio.emit('streak_update', {'with_user': user1, 'streak': streak['count']}, room=user2)
    return streak['count']

def get_recommendations(user, limit=20):
    """Рекомендации на основе лайков и избранного"""
    data = load_data()
    if user not in data['users']:
        return []
    
    liked = set(data['users'][user].get('liked_videos', []))
    favorited = set(data['users'][user].get('favorite_videos', []))
    
    # Собираем авторов из лайков и избранного
    authors = set()
    for v in data['videos']:
        if v['id'] in liked or v['id'] in favorited:
            authors.add(v['author'])
    
    # Рекомендуем видео от этих авторов, которые ещё не лайкнуты/в избранном
    recommendations = []
    for v in data['videos']:
        if v['author'] in authors and v['id'] not in liked and v['id'] not in favorited:
            recommendations.append(v)
        if len(recommendations) >= limit:
            break
    
    # Если мало рекомендаций, добиваем популярными
    if len(recommendations) < limit:
        popular = sorted(data['videos'], key=lambda x: x.get('views', 0), reverse=True)
        for v in popular:
            if v not in recommendations and v['id'] not in liked:
                recommendations.append(v)
            if len(recommendations) >= limit:
                break
    
    return recommendations

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/login', methods=['POST'])
def login():
    username = request.json.get('username', 'Аноним').strip() or 'Аноним'
    data = load_data()
    if username not in data['users']:
        data['users'][username] = {'liked_videos': [], 'favorite_videos': [], 'reposted_videos': [], 'watch_history': []}
        save_data(data)
    session['username'] = username
    return jsonify({'success': True, 'username': username})

@app.route('/api/me', methods=['GET'])
def me(): return jsonify({'username': session.get('username')})

@app.route('/api/videos', methods=['GET'])
def get_videos():
    data = load_data()
    cur = session.get('username')
    feed_type = request.args.get('feed', 'all')
    hashtag = request.args.get('hashtag', '')
    
    if feed_type == 'recommendations' and cur:
        videos = get_recommendations(cur)
    elif hashtag:
        videos = [v for v in data['videos'] if hashtag in v.get('hashtags', [])]
    else:
        videos = data.get('videos', [])
    
    for v in videos:
        v['url'] = url_for('uploaded_file', filename=v['filename'])
        if 'comments' not in v: v['comments'] = []
        if 'reposts' not in v: v['reposts'] = 0
        if 'views' not in v: v['views'] = 0
        if 'hashtags' not in v: v['hashtags'] = []
        if cur and cur in data.get('users', {}):
            u = data['users'][cur]
            v['is_liked'] = v['id'] in u.get('liked_videos', [])
            v['is_favorite'] = v['id'] in u.get('favorite_videos', [])
            v['is_reposted'] = v['id'] in u.get('reposted_videos', [])
        else:
            v['is_liked'] = v['is_favorite'] = v['is_reposted'] = False
    return jsonify(videos)

@app.route('/api/record_view/<video_id>', methods=['POST'])
def record_view(video_id):
    cur = session.get('username')
    if not cur:
        return jsonify({'error': 'No auth'}), 401
    
    data = load_data()
    for v in data['videos']:
        if v['id'] == video_id:
            v['views'] = v.get('views', 0) + 1
            if cur not in v.get('view_history', []):
                v.setdefault('view_history', []).append(cur)
            break
    
    if cur in data['users']:
        history = data['users'][cur].get('watch_history', [])
        if video_id in history:
            history.remove(video_id)
        history.insert(0, video_id)
        data['users'][cur]['watch_history'] = history[:50]  # храним последние 50
    
    save_data(data)
    return jsonify({'success': True})

@app.route('/api/watch_history', methods=['GET'])
def watch_history():
    cur = session.get('username')
    if not cur:
        return jsonify([])
    
    data = load_data()
    history_ids = data['users'].get(cur, {}).get('watch_history', [])
    videos = []
    for vid in history_ids:
        for v in data['videos']:
            if v['id'] == vid:
                v['url'] = url_for('uploaded_file', filename=v['filename'])
                videos.append(v)
                break
    return jsonify(videos)

@app.route('/api/hashtags', methods=['GET'])
def get_hashtags():
    data = load_data()
    hashtags = {}
    for v in data['videos']:
        for ht in v.get('hashtags', []):
            hashtags[ht] = hashtags.get(ht, 0) + 1
    sorted_hashtags = sorted(hashtags.items(), key=lambda x: x[1], reverse=True)
    return jsonify([{'tag': ht, 'count': c} for ht, c in sorted_hashtags[:20]])

@app.route('/api/users', methods=['GET'])
def get_users():
    users = list(load_data()['users'].keys())
    cur = session.get('username')
    if cur in users: users.remove(cur)
    return jsonify(users)

@app.route('/api/chats', methods=['GET'])
def get_chats():
    cur = session.get('username')
    if not cur: return jsonify([])
    data = load_data()
    res = []
    for cid, chat in data['chats'].items():
        if cur in chat['participants']:
            other = [u for u in chat['participants'] if u != cur][0]
            streak_key = f"{min(cur, other)}_{max(cur, other)}"
            streak = data.get('streaks', {}).get(streak_key, {}).get('count', 0)
            res.append({'id': cid, 'with_user': other, 
                       'last_message': chat['messages'][-1]['text'] if chat['messages'] else '',
                       'last_time': chat['messages'][-1]['time'] if chat['messages'] else '',
                       'streak': streak})
    return jsonify(res)

@app.route('/api/chat/<chat_id>', methods=['GET'])
def get_chat(chat_id):
    data = load_data()
    return jsonify(data['chats'].get(chat_id, {}).get('messages', []))

@app.route('/api/send_message', methods=['POST'])
def send_message():
    cur = session.get('username')
    if not cur: return jsonify({'error': 'No auth'}), 401
    data = load_data()
    target = request.json.get('target_user')
    text = request.json.get('text')
    chat_id = request.json.get('chat_id')
    
    if not chat_id:
        for cid, chat in data['chats'].items():
            if set(chat['participants']) == {cur, target}:
                chat_id = cid; break
        if not chat_id:
            chat_id = uuid.uuid4().hex
            data['chats'][chat_id] = {'id': chat_id, 'participants': [cur, target], 'messages': []}
    
    msg = {'id': uuid.uuid4().hex, 'from': cur, 'type': 'text', 'text': text, 
           'time': datetime.now().strftime('%H:%M'), 'timestamp': datetime.now().isoformat()}
    data['chats'][chat_id]['messages'].append(msg)
    save_data(data)
    
    streak = update_streak(cur, target)
    
    socketio.emit('new_message', {'chat_id': chat_id, 'message': msg, 'streak': streak}, room=target)
    return jsonify({'success': True, 'chat_id': chat_id, 'streak': streak})

@app.route('/api/share_video', methods=['POST'])
def share_video():
    cur = session.get('username')
    if not cur: return jsonify({'error': 'No auth'}), 401
    data = load_data()
    target = request.json.get('target_user')
    video_id = request.json.get('video_id')
    video = next((v for v in data['videos'] if v['id'] == video_id), None)
    if not video: return jsonify({'error': 'Video not found'}), 404
    
    chat_id = None
    for cid, chat in data['chats'].items():
        if set(chat['participants']) == {cur, target}:
            chat_id = cid; break
    if not chat_id:
        chat_id = uuid.uuid4().hex
        data['chats'][chat_id] = {'id': chat_id, 'participants': [cur, target], 'messages': []}
    
    msg = {'id': uuid.uuid4().hex, 'from': cur, 'type': 'video', 'video_id': video_id,
           'video_url': url_for('uploaded_file', filename=video['filename']),
           'video_author': video['author'], 'video_description': video['description'],
           'text': request.json.get('text', '📹 Поделился видео'),
           'time': datetime.now().strftime('%H:%M'), 'timestamp': datetime.now().isoformat()}
    data['chats'][chat_id]['messages'].append(msg)
    save_data(data)
    
    streak = update_streak(cur, target)
    
    socketio.emit('new_message', {'chat_id': chat_id, 'message': msg, 'streak': streak}, room=target)
    return jsonify({'success': True, 'chat_id': chat_id})

@app.route('/api/upload', methods=['POST'])
def upload():
    if 'video' not in request.files: return jsonify({'error': 'No file'}), 400
    f = request.files['video']
    if f.filename == '': return jsonify({'error': 'Empty'}), 400
    if not allowed_file(f.filename): return jsonify({'error': 'Format not allowed'}), 400
    
    ext = f.filename.rsplit('.',1)[1].lower()
    new_filename = f"{uuid.uuid4().hex}.{ext}"
    f.save(os.path.join(app.config['UPLOAD_FOLDER'], new_filename))
    
    description = request.form.get('description', '')
    hashtags = extract_hashtags(description)
    
    data = load_data()
    video = {'id': uuid.uuid4().hex, 'filename': new_filename, 'likes': 0, 'liked_by': [],
             'favorited_by': [], 'reposts': 0, 'reposted_by': [], 'comments': [], 
             'views': 0, 'view_history': [], 'hashtags': hashtags,
             'description': description,
             'author': request.form.get('author', 'Аноним'), 'created_at': datetime.now().isoformat()}
    data['videos'].insert(0, video)
    save_data(data)
    return jsonify({'success': True, 'video': video})

@app.route('/api/toggle_repost/<video_id>', methods=['POST'])
def toggle_repost(video_id):
    cur = session.get('username')
    if not cur: return jsonify({'error': 'No auth'}), 401
    data = load_data()
    v = next((x for x in data['videos'] if x['id'] == video_id), None)
    if not v: return jsonify({'error': 'Not found'}), 404
    if cur not in data['users']: 
        data['users'][cur] = {'liked_videos': [], 'favorite_videos': [], 'reposted_videos': [], 'watch_history': []}
    u = data['users'][cur]
    if video_id in u.get('reposted_videos', []):
        u['reposted_videos'].remove(video_id)
        v['reposts'] = max(0, v['reposts'] - 1)
        if cur in v.get('reposted_by', []): v['reposted_by'].remove(cur)
        reposted = False
    else:
        u['reposted_videos'].append(video_id)
        v['reposts'] = v.get('reposts', 0) + 1
        v.setdefault('reposted_by', []).append(cur)
        reposted = True
    save_data(data)
    socketio.emit('new_repost', {'video_id': video_id, 'reposts': v['reposts'], 'user': cur})
    return jsonify({'success': True, 'reposts': v['reposts'], 'is_reposted': reposted})

@app.route('/api/add_comment/<video_id>', methods=['POST'])
def add_comment(video_id):
    cur = session.get('username')
    if not cur: return jsonify({'error': 'No auth'}), 401
    data = load_data()
    for v in data['videos']:
        if v['id'] == video_id:
            comment = {'id': uuid.uuid4().hex, 'author': cur, 'text': request.json.get('text', ''), 
                       'created_at': uuid.uuid4().hex, 'time': datetime.now().strftime('%H:%M')}
            v.setdefault('comments', []).append(comment)
            save_data(data)
            socketio.emit('new_comment', {'video_id': video_id, 'comment': comment}, room=f'video_{video_id}')
            return jsonify({'success': True})
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/toggle_like/<video_id>', methods=['POST'])
def toggle_like(video_id):
    cur = session.get('username')
    if not cur: return jsonify({'error': 'No auth'}), 401
    data = load_data()
    v = next((x for x in data['videos'] if x['id'] == video_id), None)
    if not v: return jsonify({'error': 'Not found'}), 404
    if cur not in data['users']: 
        data['users'][cur] = {'liked_videos': [], 'favorite_videos': [], 'reposted_videos': [], 'watch_history': []}
    u = data['users'][cur]
    if video_id in u.get('liked_videos', []):
        u['liked_videos'].remove(video_id); v['likes'] = max(0, v['likes'] - 1)
        liked = False
    else:
        u['liked_videos'].append(video_id); v['likes'] = v.get('likes', 0) + 1
        liked = True
    save_data(data)
    socketio.emit('like_update', {'video_id': video_id, 'likes': v['likes']})
    return jsonify({'success': True, 'likes': v['likes'], 'is_liked': liked})

@app.route('/api/toggle_favorite/<video_id>', methods=['POST'])
def toggle_favorite(video_id):
    cur = session.get('username')
    if not cur: return jsonify({'error': 'No auth'}), 401
    data = load_data()
    v = next((x for x in data['videos'] if x['id'] == video_id), None)
    if not v: return jsonify({'error': 'Not found'}), 404
    if cur not in data['users']: 
        data['users'][cur] = {'liked_videos': [], 'favorite_videos': [], 'reposted_videos': [], 'watch_history': []}
    u = data['users'][cur]
    if video_id in u.get('favorite_videos', []):
        u['favorite_videos'].remove(video_id)
        fav = False
    else:
        u['favorite_videos'].append(video_id)
        fav = True
    save_data(data)
    socketio.emit('favorite_update', {'video_id': video_id, 'is_favorite': fav})
    return jsonify({'success': True, 'is_favorite': fav})

@app.route('/api/user_videos/<action>', methods=['GET'])
def user_videos(action):
    cur = session.get('username')
    if not cur: return jsonify([])
    data = load_data()
    if cur not in data['users']: return jsonify([])
    ids = data['users'][cur].get('liked_videos' if action == 'liked' else 'favorite_videos', [])
    videos = [v for v in data['videos'] if v['id'] in ids]
    for v in videos:
        v['url'] = url_for('uploaded_file', filename=v['filename'])
    return jsonify(videos)

@app.route('/static/uploads/<filename>')
def uploaded_file(filename): 
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@socketio.on('join')
def on_join(data):
    join_room(session.get('username'))

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
