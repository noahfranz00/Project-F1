import os
import json
from datetime import datetime
import pytz
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
import pg8000
from urllib.parse import urlparse
import anthropic

app = Flask(__name__)
CORS(app)

def get_db():
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise Exception('DATABASE_URL not set')
    result = urlparse(database_url)
    conn = pg8000.connect(
        host=result.hostname,
        port=result.port,
        user=result.username,
        password=result.password,
        database=result.path[1:]
    )
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
            id SERIAL PRIMARY KEY,
            user_message TEXT NOT NULL,
            assistant_message TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS progress_photos (
            id SERIAL PRIMARY KEY,
            image_data TEXT NOT NULL,
            photo_type TEXT NOT NULL,
            notes TEXT,
            analysis TEXT,
            weight REAL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS training_programs (
            id SERIAL PRIMARY KEY,
            program_details TEXT NOT NULL,
            week_number INTEGER,
            start_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_current BOOLEAN DEFAULT TRUE
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS nutrition_plans (
            id SERIAL PRIMARY KEY,
            plan_details TEXT NOT NULL,
            calories INTEGER,
            protein INTEGER,
            carbs INTEGER,
            fats INTEGER,
            start_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_current BOOLEAN DEFAULT TRUE
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS progress_metrics (
            id SERIAL PRIMARY KEY,
            weight REAL,
            notes TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    cursor.close()
    conn.close()

def get_context():
    conn = get_db()
    cursor = conn.cursor()
    
    context = {
        'conversations': [],
        'progress_photos': [],
        'current_program': None,
        'current_nutrition': None,
        'recent_metrics': []
    }
    
    # Helper to turn rows into dictionaries
    def make_dicts(cursor, rows):
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    # 1. Conversations
    cursor.execute('SELECT user_message, assistant_message, timestamp FROM conversations ORDER BY timestamp DESC LIMIT 30')
    convs = cursor.fetchall()
    # We use the helper to safely convert the data
    context['conversations'] = list(reversed(make_dicts(cursor, convs)))
    
    # 2. Photos
    cursor.execute('SELECT photo_type, notes, analysis, weight, timestamp FROM progress_photos ORDER BY timestamp DESC LIMIT 20')
    photos = cursor.fetchall()
    context['progress_photos'] = make_dicts(cursor, photos)
    
    # 3. Current Program
    cursor.execute('SELECT program_details, week_number, start_date FROM training_programs WHERE is_current = TRUE ORDER BY start_date DESC LIMIT 1')
    prog = cursor.fetchone()
    if prog:
        # For single items (fetchone), we wrap it in a list, convert, then take the first one
        context['current_program'] = make_dicts(cursor, [prog])[0]
    
    # 4. Nutrition
    cursor.execute('SELECT plan_details, calories, protein, carbs, fats, start_date FROM nutrition_plans WHERE is_current = TRUE ORDER BY start_date DESC LIMIT 1')
    nutr = cursor.fetchone()
    if nutr:
        context['current_nutrition'] = make_dicts(cursor, [nutr])[0]
    
    # 5. Metrics
    cursor.execute('SELECT weight, notes, timestamp FROM progress_metrics ORDER BY timestamp DESC LIMIT 10')
    metrics = cursor.fetchall()
    context['recent_metrics'] = make_dicts(cursor, metrics)
    
    cursor.close()
    conn.close()
    return context

def build_system_prompt(context):
    user_tz = pytz.timezone('America/Denver')
    now = datetime.now(user_tz)
    
    # SMART TIME LOGIC
    target_end = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now < target_end:
        total_mins = int((target_end - now).total_seconds() / 60)
        lifting_mins = max(0, total_mins - 15)
        time_context = (f"It is currently {now.strftime('%I:%M %p')}. "
                        f"Noah has {total_mins} mins total until 6:00 AM. "
                        f"Subtract 15 mins for warmup -> Design a {lifting_mins} MINUTE LIFTING SESSION.")
    else:
        time_context = (f"It is currently {now.strftime('%I:%M %p')}. "
                        f"This is outside the standard window. Ask Noah how much time he has if he didn't state it.")

    days_left = (datetime(2026, 6, 10).date() - now.date()).days

    prompt = f"""You are Noah's dedicated fitness coach.

CURRENT STATUS:
- Date: {now.strftime('%A, %B %d, %Y')}
- {time_context}
- Deadline: June 10, 2026 ({days_left} days left)

FORMATTING RULES (STRICT):
1. **PLAIN TEXT ONLY:** Do NOT use bolding (**), italics (*), or headers (##). 
2. **BAN THE ASTERISK:** Do not use the '*' character anywhere. If you want to list things, just use dashes (-) or numbers.
3. **NO SECTIONS:** Do not divide your response into "Observations", "Analysis", etc. Just write paragraphs.
4. **TEXT MESSAGE STYLE:** Write like a human texting a friend. Short paragraphs. Direct language.

YOUR PROTOCOL:
1. **Health Check:** If Noah says he is lightheaded or dizzy, prioritize health immediately (food/water/stop training).
2. **Design for the Time Available:** Fit the workout to the time calculated above.
3. **Constraints:** NO LUNGES. NO SEAFOOD.

CLIENT PROFILE:
- Weight: 235 lbs | Height: 6'0"
- Sleep: 6-7 hours (New parent - fatigue is a factor)

CONTEXT:
"""
    
    if context['current_program']:
        prog = context['current_program']
        prompt += f"\nCURRENT PROGRAM (Week {prog.get('week_number', '?')}): {prog['program_details']}\n"

    if context['current_nutrition']:
        nutr = context['current_nutrition']
        prompt += f"\nMACROS: {nutr['calories']}kcals ({nutr['protein']}p/{nutr['carbs']}c/{nutr['fats']}f)\n"
    
    if context['conversations']:
        prompt += f"\nLAST 10 MESSAGES:\n"
        for msg in context['conversations'][-10:]: 
            prompt += f"Noah: {msg['user_message']}\nYou: {msg['assistant_message']}\n"

    prompt += "\nResponse:"
def save_conversation(user_msg, assistant_msg):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO conversations (user_message, assistant_message) VALUES (%s, %s)',
        (user_msg, assistant_msg)
    )
    conn.commit()
    cursor.close()
    conn.close()

@app.route('/')
def index():
    context = get_context()
    chat_history = list(context['conversations'])
    return render_template('index.html', chat_history=chat_history)

@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get('message', '')
    image_data = data.get('image', None)
    photo_type = data.get('photo_type', 'progress')
    
    if not user_message and not image_data:
        return jsonify({'error': 'No message or image'}), 400
    
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'error': 'API key not configured'}), 500
    
    try:
        context = get_context()
        system_prompt = build_system_prompt(context)
        
        messages = []
        for conv in context['conversations']:
            messages.append({'role': 'user', 'content': conv['user_message']})
            messages.append({'role': 'assistant', 'content': conv['assistant_message']})
        
        if image_data:
            content = []
            if user_message:
                content.append({'type': 'text', 'text': user_message})
            content.append({
                'type': 'image',
                'source': {
                    'type': 'base64',
                    'media_type': 'image/jpeg',
                    'data': image_data
                }
            })
            messages.append({'role': 'user', 'content': content})
        else:
            messages.append({'role': 'user', 'content': user_message})
        
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=4000,
            system=system_prompt,
            messages=messages
        )
        
        assistant_message = response.content[0].text
        
        msg_to_save = user_message if user_message else f"[{photo_type.title()} photo uploaded]"
        save_conversation(msg_to_save, assistant_message)
        
        if image_data:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO progress_photos (image_data, photo_type, notes, analysis) VALUES (%s, %s, %s, %s)',
                (image_data, photo_type, user_message if user_message else '', assistant_message)
            )
            conn.commit()
            cursor.close()
            conn.close()
        
        return jsonify({
            'response': assistant_message,
            'timestamp': datetime.now().isoformat()
        })
    
    except Exception as e:
        print(f"Error: {str(e)}")
        return jsonify({'error': str(e)}), 500

try:
    init_db()
    print("Database initialized")
except Exception as e:
    print(f"DB init error: {e}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
