from flask import Flask, request, jsonify, render_template, redirect, session
from flask_session import Session
import psycopg2
import bcrypt
import os, base64
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import load_der_public_key

SESSION_FILE_DIR = os.path.join(os.getcwd(), 'flask_session')
os.makedirs(SESSION_FILE_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = 'e8b5a51ad65b4f8d9c923e3c9a79d612'
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = SESSION_FILE_DIR
Session(app)

USER_DB_CONFIG = {
    "dbname": "userdb",
    "user": "messagist_user",
    "password": "secure_password",
    "host": "192.168.1.20",
    "port": 5432,
}
MESSAGES_DB_CONFIG = {
    "dbname": "messagesdb",
    "user": "messagist_user",
    "password": "secure_password",
    "host": "192.168.1.20",
    "port": 5432,
}

def get_userdb_connection():
    return psycopg2.connect(**USER_DB_CONFIG)

def get_messagesdb_connection():
    return psycopg2.connect(**MESSAGES_DB_CONFIG)

@app.route('/')
def login():
    return render_template('login.html')

@app.route('/chat')
def chat():
    if 'istid' not in session:
        return redirect('/')
    return render_template('chat.html')

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    istid = data['istid']
    password = data['password']
    public_key = data['public_key']

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    conn = get_userdb_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users (istid, password_hash, public_key)
        VALUES (%s, %s, %s)
        """,
        (istid, password_hash, public_key)
    )
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"message": "User registered successfully"}), 201

@app.route('/login', methods=['POST'])
def handle_login():
    if not request.is_json:
        return jsonify({"error": "Unsupported Media Type"}), 415

    istid = request.json.get('istid')
    password = request.json.get('password')

    conn = get_userdb_connection()
    cur = conn.cursor()
    cur.execute("SELECT password_hash FROM users WHERE istid = %s", (istid,))
    result = cur.fetchone()
    cur.close()
    conn.close()

    if not result:
        return jsonify({"error": "Invalid IST ID"}), 401

    stored_password_hash = result[0]

    if not bcrypt.checkpw(password.encode('utf-8'), stored_password_hash.encode('utf-8')):
        return jsonify({"error": "Invalid password"}), 401

    session['istid'] = istid
    return jsonify({"message": "Login successful"}), 200

@app.route('/logout', methods=['POST'])
def handle_logout():
    session.clear()
    return redirect('/')

def get_or_create_conversation(participantUm, participantDois):
    try:
        conn = get_messagesdb_connection()
        cur = conn.cursor()

        participant1 = min(participantUm, participantDois)
        participant2 = max(participantUm, participantDois)

        cur.execute(
            "SELECT id, encrypted_sessionkey_sender, encrypted_sessionkey_receiver FROM conversations WHERE participant1_istid=%s AND participant2_istid=%s",
            (participant1, participant2)
        )
        result = cur.fetchone()

        if result is None:
            # Create conversation
            # Fetch public keys
            conn_users = get_userdb_connection()
            cur_users = conn_users.cursor()
            cur_users.execute("SELECT public_key FROM users WHERE istid=%s", (participant1,))
            p1_key_der = cur_users.fetchone()[0]
            cur_users.execute("SELECT public_key FROM users WHERE istid=%s", (participant2,))
            p2_key_der = cur_users.fetchone()[0]
            conn_users.close()

            p1_pub = load_der_public_key(base64.b64decode(p1_key_der))
            p2_pub = load_der_public_key(base64.b64decode(p2_key_der))

            # Generate session key
            session_key = os.urandom(32)

            encrypted_sessionkey_sender = p1_pub.encrypt(
                session_key,
                padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                             algorithm=hashes.SHA256(),
                             label=None)
            )
            encrypted_sessionkey_receiver = p2_pub.encrypt(
                session_key,
                padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                             algorithm=hashes.SHA256(),
                             label=None)
            )

            cur.execute(
                """
                INSERT INTO conversations(participant1_istid, participant2_istid, encrypted_sessionkey_sender, encrypted_sessionkey_receiver)
                VALUES(%s,%s,%s,%s) RETURNING id
                """,
                (participant1, participant2,
                 base64.b64encode(encrypted_sessionkey_sender).decode(),
                 base64.b64encode(encrypted_sessionkey_receiver).decode())
            )
            result = cur.fetchone()

        conn.commit()
        cur.close()
        conn.close()
        return result[0]

    except Exception as e:
        print("ERROR in get_or_create_conversation:", e)
        return None

@app.route('/get_conversation_keys', methods=['POST'])
def get_conversation_keys():
    if 'istid' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    sender_id = int(session['istid'])
    receiver_id = int(data['receiver_istid'])

    conversation_id = get_or_create_conversation(sender_id, receiver_id)
    if not conversation_id:
        return jsonify({"error": "Failed to get conversation"}), 500

    conn = get_messagesdb_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT encrypted_sessionkey_sender, encrypted_sessionkey_receiver, participant1_istid, participant2_istid FROM conversations WHERE id=%s",
        (conversation_id,)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        return jsonify({"error": "Conversation not found"}), 404

    enc_key_sender, enc_key_receiver, p1, p2 = row

    # Determine which encrypted key to return
    logged_in_user = sender_id
    if logged_in_user == p1:
        enc_key_for_user = enc_key_sender
    else:
        enc_key_for_user = enc_key_receiver

    return jsonify({
        "encrypted_sessionkey_for_user": enc_key_for_user,
        "conversation_id": conversation_id
    }), 200

@app.route('/get_active_conversations', methods=['POST'])
def get_active_conversations():
    if 'istid' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_messagesdb_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, participant1_istid, participant2_istid FROM conversations")
        rows = cur.fetchall()
        cur.close()
        conn.close()

        active_conversations = [{
            "conversation_id": r[0],
            "participants": [r[1], r[2]]
        } for r in rows]

        return jsonify({"active_conversations": active_conversations}), 200
    except Exception as e:
        print("Error:", e)
        return jsonify({"error": str(e)}), 500

@app.route('/get_user_public_key', methods=['POST'])
def get_user_public_key():
    if not request.is_json:
        return jsonify({"error": "Invalid request"}), 400
    data = request.json
    istid = data.get('istid')
    try:
        conn = get_userdb_connection()
        cur = conn.cursor()
        cur.execute("SELECT public_key FROM users WHERE istid=%s", (istid,))
        result = cur.fetchone()
        cur.close()
        conn.close()
        if not result:
            return jsonify({"error": "User not found"}), 404
        return jsonify({"public_key": result[0]}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/send_message', methods=['POST'])
def send_message():
    if 'istid' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json
    sender_id = int(session['istid'])
    receiver_id = int(data['receiver_istid'])
    encrypted_content = data['encrypted_content']
    iv = data['iv']

    # Get or create the conversation
    conversation_id = get_or_create_conversation(sender_id, receiver_id)
    if not conversation_id:
        return jsonify({"error": "Failed to fetch or create conversation"}), 500

    try:
        conn = get_messagesdb_connection()
        cur = conn.cursor()

        # Get the current max sequence number for this conversation
        cur.execute(
            """
            SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next_sequence_number
            FROM messages
            WHERE conversation_id = %s
            """,
            (conversation_id,)
        )
        next_sequence_number = cur.fetchone()[0]

        # Insert the message with the calculated sequence number
        cur.execute(
            """
            INSERT INTO messages (conversation_id, sender_istid, receiver_istid, timestamp, ciphertext, iv, sequence_number)
            VALUES (%s, %s, %s, NOW(), %s, %s, %s)
            """,
            (conversation_id, sender_id, receiver_id, encrypted_content, iv, next_sequence_number)
        )
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({"message": "Message sent successfully", "sequence_number": next_sequence_number}), 201
    except Exception as e:
        print("Error:", e)
        return jsonify({"error": str(e)}), 500


@app.route('/fetch_messages', methods=['POST'])
def fetch_messages():
    if 'istid' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    sender_id = int(session['istid'])
    receiver_id = int(data['receiver_istid'])

    # Get or create the conversation
    conversation_id = get_or_create_conversation(sender_id, receiver_id)
    if not conversation_id:
        return jsonify({"error": "Failed to fetch conversation"}), 500

    try:
        conn = get_messagesdb_connection()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT sequence_number, sender_istid, timestamp, ciphertext, iv
            FROM messages
            WHERE conversation_id = %s
            ORDER BY sequence_number
            """,
            (conversation_id,)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()

        messages = []
        for seq_num, sender, timestamp, ciphertext, iv in rows:
            messages.append({
                "sequence_number": seq_num,
                "timestamp": timestamp.isoformat(),
                "sender": sender,
                "encrypted_content": ciphertext,
                "iv": iv
            })

        return jsonify({"messages": messages}), 200
    except Exception as e:
        print("Error:", e)
        return jsonify({"error": str(e)}), 500

@app.route('/get_logged_in_user', methods=['GET'])
def get_logged_in_user():
    if 'istid' not in session:
        return jsonify({"error": "Unauthorized access"}), 401
    return jsonify({"istid": session['istid']}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
