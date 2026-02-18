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
    
    hour = now.hour
    if 5 <= hour < 11:
        time_period = "Morning"
    elif 11 <= hour < 14:
        time_period = "Midday"
    elif 14 <= hour < 18:
        time_period = "Afternoon"
    elif 18 <= hour < 22:
        time_period = "Evening"
    else:
        time_period = "Night"
    
    prompt = f"""You are an elite personal trainer and nutrition coach. Your client is Noah.

CURRENT DATE/TIME:
{now.strftime('%A, %B %d, %Y')} at {now.strftime('%I:%M %p %Z')}
Time of Day: {time_period}

CLIENT PROFILE:
- Age: 28
- Current Weight: 235 lbs
- Height: 6'0"
- Training Experience: 13 years (advanced lifter)
- Sleep: 6-7 hours interrupted (new parent - factor this into recovery)
- Goal: Get lean and shredded by June 10, 2026 (deadline: {(datetime(2026, 6, 10).date() - now.date()).days} days from now)
- Post-June 10: Maintain lean physique

TRAINING SCHEDULE:
- 4 days per week: Tuesday, Wednesday, Thursday, Friday
- Time: 4:30 AM - 6:00 AM (90 minutes including warmup)
- Location: Commercial gym (full equipment access)
- Restrictions: NO LUNGES (client preference)

NUTRITION:
- Dietary Restriction: NO SEAFOOD
- You must provide specific macros (protein, carbs, fats, calories)
- Adjust based on progress photos and weekly check-ins

CRITICAL INSTRUCTIONS:
1. REMEMBER EVERYTHING - You have full access to all past conversations, photos, programs, and metrics
2. ADAPT BASED ON PROGRESS - Adjust training and nutrition when progress stalls or accelerates
3. RESPECT TIME CONSTRAINTS - Workouts must fit in 90 minutes (warmup included)
4. BE RESULTS-DRIVEN - Noah has 13 years experience, give him advanced programming
5. ACCOUNT FOR SLEEP - With interrupted sleep, manage volume and intensity carefully
6. TRACK TOWARD DEADLINE - June 10 is non-negotiable, adjust plan to hit that date

PHOTO ANALYSIS:
- Progress photos: Assess physique changes, body composition, muscle development
- Meal photos: Estimate macros as accurately as possible (protein/carbs/fats/calories)

PROGRAMMING PRINCIPLES:
- 90-minute sessions means: 10min warmup, 60-70min main work, 10min accessories/cooldown
- Advanced lifter programming: periodization, progressive overload, deload weeks
- Adjust volume/intensity based on sleep quality and recovery feedback
- No lunges ever - use alternatives (split squats, step-ups, etc.)

"""
    
    if context['current_program']:
        prog = context['current_program']
        prompt += f"\n\nCURRENT TRAINING PROGRAM (Week {prog.get('week_number', 'N/A')}):\n"
        prompt += f"Started: {prog['start_date']}\n"
        prompt += f"{prog['program_details']}\n"
    
    if context['current_nutrition']:
        nutr = context['current_nutrition']
        prompt += f"\n\nCURRENT NUTRITION PLAN:\n"
        prompt += f"Calories: {nutr['calories']} | Protein: {nutr['protein']}g | Carbs: {nutr['carbs']}g | Fats: {nutr['fats']}g\n"
        prompt += f"{nutr['plan_details']}\n"
    
    if context['progress_photos']:
        prompt += f"\n\nPROGRESS PHOTO HISTORY ({len([p for p in context['progress_photos'] if p['photo_type'] == 'progress'])} photos):\n"
        for photo in context['progress_photos']:
            if photo['photo_type'] == 'progress':
                prompt += f"[{photo['timestamp']}] Weight: {photo.get('weight', 'N/A')} lbs"
                if photo.get('analysis'):
                    prompt += f" - {photo['analysis'][:200]}"
                prompt += "\n"
    
    if context['recent_metrics']:
        prompt += f"\n\nRECENT PROGRESS METRICS:\n"
        for m in context['recent_metrics'][:5]:
            prompt += f"[{m['timestamp']}] Weight: {m['weight']} lbs - {m.get('notes', '')}\n"
    
    if context['conversations']:
        prompt += f"\n\nYou have access to the last 30 conversations below.\n"
    
    prompt += "\n\nBEFORE RESPONDING: Verify your recommendation fits Noah's 90-minute window, respects his constraints, and moves him toward his June 10 deadline.\n"
    
    return prompt

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
