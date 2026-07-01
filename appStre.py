import os
import json
import uuid
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, request, jsonify, g, render_template_string
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from groq import Groq
from dotenv import load_dotenv
import statistics
import re
import math
from collections import Counter

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Initialize rate limiter
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

# Initialize Groq client
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# Database setup
DATABASE = 'audit_log.db'

def get_db():
    """Get database connection for the current request."""
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db

def close_db(e=None):
    """Close database connection."""
    db = g.pop('db', None)
    if db is not None:
        db.close()

app.teardown_appcontext(close_db)

def init_db():
    """Initialize the database with required tables."""
    with app.app_context():
        db = get_db()
        
        # Main audit log table
        db.execute('''
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_id TEXT NOT NULL,
                creator_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                text_content TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'text',
                attribution TEXT NOT NULL,
                confidence REAL NOT NULL,
                llm_score REAL NOT NULL,
                stylometric_score REAL NOT NULL,
                burstiness_score REAL,
                status TEXT NOT NULL DEFAULT 'classified',
                appeal_reasoning TEXT,
                label_text TEXT NOT NULL,
                verified_human INTEGER DEFAULT 0
            )
        ''')
        
        # Provenance certificates table
        db.execute('''
            CREATE TABLE IF NOT EXISTS provenance_certificates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id TEXT NOT NULL UNIQUE,
                verified_at TEXT NOT NULL,
                verification_method TEXT NOT NULL,
                certificate_id TEXT NOT NULL UNIQUE
            )
        ''')
        
        db.commit()

def log_to_db(content_id, creator_id, text, attribution, confidence, 
              llm_score, stylometric_score, burstiness_score=None,
              content_type="text", status="classified", 
              appeal_reasoning=None, label_text="", verified_human=0):
    """Write an entry to the audit log database."""
    db = get_db()
    timestamp = datetime.now(timezone.utc).isoformat()
    db.execute('''
        INSERT INTO audit_log 
        (content_id, creator_id, timestamp, text_content, content_type,
         attribution, confidence, llm_score, stylometric_score, burstiness_score,
         status, appeal_reasoning, label_text, verified_human)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (content_id, creator_id, timestamp, text, content_type,
          attribution, confidence, llm_score, stylometric_score, burstiness_score,
          status, appeal_reasoning, label_text, verified_human))
    db.commit()

def get_log_entries(limit=50):
    """Retrieve the most recent audit log entries."""
    db = get_db()
    entries = db.execute(
        'SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?', 
        (limit,)
    ).fetchall()
    return [dict(entry) for entry in entries]

def update_content_status(content_id, new_status):
    """Update the status of a content entry."""
    db = get_db()
    db.execute(
        'UPDATE audit_log SET status = ? WHERE content_id = ?',
        (new_status, content_id)
    )
    db.commit()

def get_entry_by_content_id(content_id):
    """Retrieve a specific audit log entry by content_id."""
    db = get_db()
    entry = db.execute(
        'SELECT * FROM audit_log WHERE content_id = ? ORDER BY timestamp DESC LIMIT 1',
        (content_id,)
    ).fetchone()
    return dict(entry) if entry else None

def is_verified_human(creator_id):
    """Check if a creator has a provenance certificate."""
    db = get_db()
    cert = db.execute(
        'SELECT * FROM provenance_certificates WHERE creator_id = ?',
        (creator_id,)
    ).fetchone()
    return cert is not None

def issue_certificate(creator_id, verification_method="writing_sample"):
    """Issue a provenance certificate to a verified creator."""
    db = get_db()
    certificate_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()
    
    db.execute('''
        INSERT OR REPLACE INTO provenance_certificates 
        (creator_id, verified_at, verification_method, certificate_id)
        VALUES (?, ?, ?, ?)
    ''', (creator_id, timestamp, verification_method, certificate_id))
    db.commit()
    
    return certificate_id

# Signal 1: Groq LLM-based detection
def analyze_with_llm(text):
    """
    Use Groq LLM to assess if text appears AI-generated.
    Returns a score between 0 and 1 (higher = more AI-like).
    """
    prompt = f"""Analyze the following text and determine if it appears to be written by AI or a human. 
Consider factors like:
- Natural variation in sentence structure
- Presence of personal voice or idiosyncratic expression
- Use of formulaic phrases or clichés
- Coherence patterns typical of AI generation

Return ONLY a number between 0 and 1, where:
- 0.0-0.3: Definitely human-written
- 0.3-0.7: Uncertain/ambiguous
- 0.7-1.0: Likely AI-generated

Text to analyze:
{text}

Score:"""

    try:
        response = groq_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": "You are an AI text detection expert. Respond only with a numeric score."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            model="llama-3.3-70b-versatile",
            temperature=0.1,
            max_tokens=10,
        )
        
        # Extract numeric score from response
        score_text = response.choices[0].message.content.strip()
        # Extract first floating point number from response
        match = re.search(r'(\d+\.?\d*)', score_text)
        if match:
            score = float(match.group(1))
            return max(0.0, min(1.0, score))  # Clamp between 0 and 1
        else:
            return 0.5  # Default to uncertain if parsing fails
            
    except Exception as e:
        print(f"Groq API error: {e}")
        return 0.5  # Default to uncertain on error

# Signal 2: Stylometric analysis
def analyze_stylometric(text):
    """
    Analyze statistical properties of text that differ between human and AI writing.
    Returns a score between 0 and 1 (higher = more AI-like patterns).
    """
    if not text or len(text.strip()) < 50:
        return 0.5  # Insufficient text for analysis
    
    # Split into sentences
    sentences = re.split(r'[.!?]+', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    
    if len(sentences) < 2:
        return 0.5  # Need multiple sentences for analysis
    
    # 1. Sentence length variance (AI text tends to be more uniform)
    sentence_lengths = [len(s.split()) for s in sentences]
    if len(sentence_lengths) > 1:
        length_variance = statistics.stdev(sentence_lengths) / max(statistics.mean(sentence_lengths), 1)
    else:
        length_variance = 0
    
    # 2. Type-token ratio (vocabulary diversity - AI often uses more repeated vocabulary)
    words = re.findall(r'\b\w+\b', text.lower())
    if len(words) > 0:
        type_token_ratio = len(set(words)) / len(words)
    else:
        type_token_ratio = 0
    
    # 3. Punctuation density
    punctuation_count = len(re.findall(r'[.,;:!?—""''()]', text))
    punctuation_density = punctuation_count / max(len(sentences), 1)
    
    # Combine metrics into a single score
    # AI text typically has: low variance, low TTR, moderate punctuation
    variance_score = max(0, min(1, 1 - length_variance))  # Low variance = higher score
    ttr_score = max(0, min(1, 1 - type_token_ratio * 2))  # Low TTR = higher score (inverted and scaled)
    punct_score = max(0, min(1, abs(punctuation_density - 2) / 3))  # Deviation from "natural" density
    
    # Weighted combination of metrics
    combined_score = (variance_score * 0.4 + ttr_score * 0.4 + punct_score * 0.2)
    
    return max(0.0, min(1.0, combined_score))

# Signal 3: Burstiness Analysis (Ensemble Detection)
def analyze_burstiness(text):
    """
    Analyze lexical burstiness patterns that differ between human and AI writing.
    Human writing shows more variation in sentence complexity (burstiness).
    Returns a score between 0 and 1 (higher = more AI-like, less bursty).
    """
    if not text or len(text.strip()) < 50:
        return 0.5
    
    sentences = re.split(r'[.!?]+', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    
    if len(sentences) < 3:
        return 0.5
    
    # Calculate complexity score for each sentence
    complexity_scores = []
    for sentence in sentences:
        words = sentence.split()
        if not words:
            continue
        
        # Complexity factors
        word_length_avg = sum(len(w) for w in words) / len(words)
        unique_ratio = len(set(words)) / len(words)
        complexity = word_length_avg * unique_ratio
        complexity_scores.append(complexity)
    
    if len(complexity_scores) < 2:
        return 0.5
    
    # Calculate burstiness (variation in complexity between consecutive sentences)
    burstiness_values = []
    for i in range(len(complexity_scores) - 1):
        burst = abs(complexity_scores[i] - complexity_scores[i + 1])
        burstiness_values.append(burst)
    
    avg_burstiness = statistics.mean(burstiness_values) if burstiness_values else 0
    
    # AI text tends to have lower burstiness (more uniform complexity)
    # Normalize to 0-1 scale (higher = more AI-like)
    burstiness_score = max(0, min(1, 1 - avg_burstiness * 3))
    
    return burstiness_score

def combine_signals(llm_score, stylometric_score, burstiness_score=None):
    """
    Combine detection signals into a single confidence score.
    With ensemble: 45% LLM, 30% Stylometric, 25% Burstiness
    Without ensemble: 60% LLM, 40% Stylometric
    """
    if burstiness_score is not None:
        # Ensemble mode with conflict resolution
        scores = [llm_score, stylometric_score, burstiness_score]
        weights = [0.45, 0.30, 0.25]
        
        # Check for signal conflicts (disagreement > 0.3)
        max_diff = max(scores) - min(scores)
        if max_diff > 0.3:
            # High conflict - reduce confidence toward uncertain
            weighted_score = sum(s * w for s, w in zip(scores, weights))
            # Pull toward 0.5 (uncertain) proportionally to conflict
            conflict_factor = min(1.0, (max_diff - 0.3) / 0.4)
            adjusted_score = weighted_score * (1 - conflict_factor) + 0.5 * conflict_factor
            return adjusted_score
        
        return sum(s * w for s, w in zip(scores, weights))
    else:
        # Standard two-signal mode
        return (llm_score * 0.6) + (stylometric_score * 0.4)

def generate_label(confidence_score, verified_human=False, content_type="text"):
    """
    Map confidence score to appropriate transparency label.
    Returns both the attribution category and label text.
    """
    if verified_human:
        verified_prefix = "✅ Verified Human Creator | "
    else:
        verified_prefix = ""
    
    if content_type == "image_description":
        content_label = "image description"
    else:
        content_label = "content"
    
    if confidence_score > 0.65:
        attribution = "likely_ai"
        label_text = (
            f"{verified_prefix}Our analysis suggests this {content_label} may have been AI-generated. "
            "While our detection systems show a high degree of confidence, "
            "automated tools can make mistakes. If you're the creator and "
            "believe this is an error, you can appeal this classification."
        )
    elif confidence_score >= 0.4:
        attribution = "uncertain"
        label_text = (
            f"{verified_prefix}We're unable to confidently determine whether this {content_label} was "
            "written by a human or generated by AI. Some patterns are ambiguous, "
            "which is common with certain writing styles. The creator can provide "
            "additional context to help clarify."
        )
    else:
        attribution = "likely_human"
        label_text = (
            f"{verified_prefix}Our analysis suggests this {content_label} was likely written by a human. "
            "The writing shows natural variation and patterns consistent with "
            "human authorship. No further verification is needed at this time."
        )
    
    return attribution, label_text

@app.route('/submit', methods=['POST'])
@limiter.limit("10 per minute;100 per day")
def submit_content():
    """
    Submit content for AI attribution analysis.
    Supports text and image_description content types.
    Expects JSON with 'text', 'creator_id', and optional 'content_type' fields.
    """
    try:
        data = request.get_json()
        
        if not data or 'text' not in data or 'creator_id' not in data:
            return jsonify({
                'error': 'Missing required fields. Please provide text and creator_id.'
            }), 400
        
        text = data['text']
        creator_id = data['creator_id']
        content_type = data.get('content_type', 'text')
        
        if content_type not in ['text', 'image_description']:
            return jsonify({
                'error': 'Invalid content_type. Must be "text" or "image_description".'
            }), 400
        
        if not text or len(text.strip()) < 10:
            return jsonify({
                'error': 'Content must be at least 10 characters long.'
            }), 400
        
        # Generate unique content ID
        content_id = str(uuid.uuid4())
        
        # Check for verified human status
        verified_human = is_verified_human(creator_id)
        
        # Run detection signals
        llm_score = analyze_with_llm(text)
        stylometric_score = analyze_stylometric(text)
        burstiness_score = analyze_burstiness(text)  # Ensemble signal
        
        # Combine signals into confidence score (using ensemble)
        confidence = combine_signals(llm_score, stylometric_score, burstiness_score)
        
        # Generate appropriate label
        attribution, label_text = generate_label(confidence, verified_human, content_type)
        
        # Log the classification
        log_to_db(
            content_id=content_id,
            creator_id=creator_id,
            text=text,
            attribution=attribution,
            confidence=confidence,
            llm_score=llm_score,
            stylometric_score=stylometric_score,
            burstiness_score=burstiness_score,
            content_type=content_type,
            label_text=label_text,
            verified_human=1 if verified_human else 0
        )
        
        response_data = {
            'content_id': content_id,
            'attribution': attribution,
            'confidence': round(confidence, 4),
            'llm_score': round(llm_score, 4),
            'stylometric_score': round(stylometric_score, 4),
            'burstiness_score': round(burstiness_score, 4),
            'label': label_text,
            'verified_human': verified_human
        }
        
        return jsonify(response_data), 200
        
    except Exception as e:
        return jsonify({
            'error': f'Internal server error: {str(e)}'
        }), 500

@app.route('/verify-human', methods=['POST'])
def verify_human_creator():
    """
    Verify a human creator and issue a provenance certificate.
    Requires creator_id and writing_sample for verification.
    """
    try:
        data = request.get_json()
        
        if not data or 'creator_id' not in data or 'writing_sample' not in data:
            return jsonify({
                'error': 'Missing required fields. Please provide creator_id and writing_sample.'
            }), 400
        
        creator_id = data['creator_id']
        writing_sample = data['writing_sample']
        
        # Verify the writing sample shows human characteristics
        llm_score = analyze_with_llm(writing_sample)
        stylometric_score = analyze_stylometric(writing_sample)
        burstiness_score = analyze_burstiness(writing_sample)
        
        confidence = combine_signals(llm_score, stylometric_score, burstiness_score)
        
        # Only verify if confidence is low (likely human)
        if confidence > 0.5:
            return jsonify({
                'error': 'Verification failed. The writing sample shows AI-like patterns.',
                'confidence': round(confidence, 4),
                'message': 'Please submit a more natural, personal writing sample.'
            }), 400
        
        # Issue certificate
        certificate_id = issue_certificate(creator_id)
        
        return jsonify({
            'status': 'verified',
            'creator_id': creator_id,
            'certificate_id': certificate_id,
            'message': 'You have been verified as a human creator! Your content will now display the Verified Human badge.',
            'confidence': round(confidence, 4)
        }), 200
        
    except Exception as e:
        return jsonify({
            'error': f'Verification error: {str(e)}'
        }), 500

@app.route('/appeal', methods=['POST'])
def appeal_classification():
    """
    Submit an appeal for a content classification.
    Expects JSON with 'content_id' and 'creator_reasoning' fields.
    """
    try:
        data = request.get_json()
        
        if not data or 'content_id' not in data or 'creator_reasoning' not in data:
            return jsonify({
                'error': 'Missing required fields. Please provide content_id and creator_reasoning.'
            }), 400
        
        content_id = data['content_id']
        creator_reasoning = data['creator_reasoning']
        
        # Look up original classification
        original_entry = get_entry_by_content_id(content_id)
        
        if not original_entry:
            return jsonify({
                'error': 'Content ID not found. Please check the ID and try again.'
            }), 404
        
        if original_entry['status'] == 'under_review':
            return jsonify({
                'error': 'This content is already under review.'
            }), 400
        
        # Update status to under review
        update_content_status(content_id, 'under_review')
        
        # Create appeal log entry
        log_to_db(
            content_id=content_id,
            creator_id=original_entry['creator_id'],
            text=original_entry['text_content'],
            attribution=original_entry['attribution'],
            confidence=original_entry['confidence'],
            llm_score=original_entry['llm_score'],
            stylometric_score=original_entry['stylometric_score'],
            burstiness_score=original_entry.get('burstiness_score'),
            content_type=original_entry.get('content_type', 'text'),
            status='under_review',
            appeal_reasoning=creator_reasoning,
            label_text=original_entry['label_text'],
            verified_human=original_entry.get('verified_human', 0)
        )
        
        return jsonify({
            'status': 'appeal_received',
            'content_id': content_id,
            'new_status': 'under_review',
            'message': 'Your appeal has been received and will be reviewed by our team.'
        }), 200
        
    except Exception as e:
        return jsonify({
            'error': f'Internal server error: {str(e)}'
        }), 500

@app.route('/dashboard')
def analytics_dashboard():
    """
    Analytics dashboard showing detection patterns, appeal rates, and metrics.
    """
    db = get_db()
    
    # Total submissions
    total = db.execute('SELECT COUNT(DISTINCT content_id) as count FROM audit_log').fetchone()['count']
    
    # Attribution distribution
    ai_count = db.execute(
        "SELECT COUNT(DISTINCT content_id) as count FROM audit_log WHERE attribution = 'likely_ai'"
    ).fetchone()['count']
    
    human_count = db.execute(
        "SELECT COUNT(DISTINCT content_id) as count FROM audit_log WHERE attribution = 'likely_human'"
    ).fetchone()['count']
    
    uncertain_count = db.execute(
        "SELECT COUNT(DISTINCT content_id) as count FROM audit_log WHERE attribution = 'uncertain'"
    ).fetchone()['count']
    
    # Appeal rate
    appeals = db.execute(
        "SELECT COUNT(DISTINCT content_id) as count FROM audit_log WHERE status = 'under_review'"
    ).fetchone()['count']
    
    appeal_rate = (appeals / total * 100) if total > 0 else 0
    
    # Average confidence by attribution
    avg_confidence_ai = db.execute(
        "SELECT AVG(confidence) as avg FROM audit_log WHERE attribution = 'likely_ai'"
    ).fetchone()['avg'] or 0
    
    avg_confidence_human = db.execute(
        "SELECT AVG(confidence) as avg FROM audit_log WHERE attribution = 'likely_human'"
    ).fetchone()['avg'] or 0
    
    # Verified human count
    verified_count = db.execute(
        "SELECT COUNT(DISTINCT creator_id) as count FROM provenance_certificates"
    ).fetchone()['count']
    
    # Content type distribution
    text_count = db.execute(
        "SELECT COUNT(DISTINCT content_id) as count FROM audit_log WHERE content_type = 'text'"
    ).fetchone()['count']
    
    image_desc_count = db.execute(
        "SELECT COUNT(DISTINCT content_id) as count FROM audit_log WHERE content_type = 'image_description'"
    ).fetchone()['count']
    
    # Recent activity
    recent = db.execute(
        'SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT 10'
    ).fetchall()

    # Precompute percentages (avoids format-spec issues inside the f-string)
    ai_pct = (ai_count / total * 100) if total > 0 else 0
    human_pct = (human_count / total * 100) if total > 0 else 0
    uncertain_pct = (uncertain_count / total * 100) if total > 0 else 0

    # Build the recent-activity rows separately (avoids nested f-strings)
    recent_rows = ""
    for entry in recent:
        badge_class = entry['attribution'].replace('likely_', '')
        verified_badge = '<span class="badge verified">✓ Verified</span>' if entry.get('verified_human') else ''
        snippet = entry['text_content'][:80]
        ts = entry['timestamp'][:19]
        recent_rows += (
            '<div class="activity-item">'
            f'<span class="badge {badge_class}">{entry["attribution"]}</span>'
            f'{verified_badge}'
            f'<span style="margin-left: 10px;">{snippet}...</span>'
            f'<span class="timestamp" style="float: right;">{ts}</span>'
            '</div>'
        )

    dashboard_html = f'''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Provenance Guard - Analytics Dashboard</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; background: #f5f5f5; padding: 20px; }}
            .dashboard {{ max-width: 1200px; margin: 0 auto; }}
            h1 {{ color: #333; margin-bottom: 30px; font-size: 2em; }}
            .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin-bottom: 30px; }}
            .metric-card {{ background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
            .metric-card h3 {{ color: #666; font-size: 0.9em; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.5px; }}
            .metric-value {{ font-size: 2.5em; font-weight: bold; color: #333; }}
            .metric-subtitle {{ color: #888; font-size: 0.9em; margin-top: 5px; }}
            .chart-container {{ background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px; }}
            .bar-chart {{ display: flex; align-items: flex-end; height: 200px; gap: 40px; padding: 20px 0; }}
            .bar {{ flex: 1; max-width: 100px; position: relative; border-radius: 5px 5px 0 0; transition: height 0.3s; }}
            .bar-label {{ text-align: center; margin-top: 10px; font-size: 0.9em; color: #666; }}
            .bar-value {{ position: absolute; top: -25px; width: 100%; text-align: center; font-weight: bold; color: #333; }}
            .bar.ai {{ background: linear-gradient(to top, #ff6b6b, #ff8787); height: {ai_pct}%; }}
            .bar.human {{ background: linear-gradient(to top, #51cf66, #69db7c); height: {human_pct}%; }}
            .bar.uncertain {{ background: linear-gradient(to top, #ffd43b, #ffe066); height: {uncertain_pct}%; }}
            .recent-activity {{ background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
            .activity-item {{ padding: 10px; border-bottom: 1px solid #f0f0f0; }}
            .activity-item:last-child {{ border-bottom: none; }}
            .badge {{ display: inline-block; padding: 3px 8px; border-radius: 12px; font-size: 0.8em; font-weight: bold; }}
            .badge.ai {{ background: #ffe0e0; color: #cc0000; }}
            .badge.human {{ background: #e0ffe0; color: #006600; }}
            .badge.uncertain {{ background: #fff3cd; color: #856404; }}
            .badge.verified {{ background: #e0e0ff; color: #0000cc; }}
            .timestamp {{ color: #999; font-size: 0.9em; }}
        </style>
    </head>
    <body>
        <div class="dashboard">
            <h1>📊 Provenance Guard Analytics</h1>
            
            <div class="metrics-grid">
                <div class="metric-card">
                    <h3>Total Submissions</h3>
                    <div class="metric-value">{total}</div>
                    <div class="metric-subtitle">Content pieces analyzed</div>
                </div>
                
                <div class="metric-card">
                    <h3>Appeal Rate</h3>
                    <div class="metric-value">{appeal_rate:.1f}%</div>
                    <div class="metric-subtitle">{appeals} appeals filed</div>
                </div>
                
                <div class="metric-card">
                    <h3>Verified Humans</h3>
                    <div class="metric-value">{verified_count}</div>
                    <div class="metric-subtitle">Creators with certificates</div>
                </div>
                
                <div class="metric-card">
                    <h3>Content Types</h3>
                    <div class="metric-value">{text_count + image_desc_count}</div>
                    <div class="metric-subtitle">Text: {text_count} | Image Desc: {image_desc_count}</div>
                </div>
            </div>
            
            <div class="chart-container">
                <h3>Detection Distribution</h3>
                <div class="bar-chart">
                    <div style="text-align: center; flex: 1; max-width: 100px;">
                        <div class="bar ai">
                            <div class="bar-value">{ai_count}</div>
                        </div>
                        <div class="bar-label">AI ({ai_pct:.0f}%)</div>
                    </div>
                    <div style="text-align: center; flex: 1; max-width: 100px;">
                        <div class="bar human">
                            <div class="bar-value">{human_count}</div>
                        </div>
                        <div class="bar-label">Human ({human_pct:.0f}%)</div>
                    </div>
                    <div style="text-align: center; flex: 1; max-width: 100px;">
                        <div class="bar uncertain">
                            <div class="bar-value">{uncertain_count}</div>
                        </div>
                        <div class="bar-label">Uncertain ({uncertain_pct:.0f}%)</div>
                    </div>
                </div>
            </div>
            
            <div class="metrics-grid">
                <div class="metric-card">
                    <h3>Avg Confidence (AI)</h3>
                    <div class="metric-value">{avg_confidence_ai:.2f}</div>
                    <div class="metric-subtitle">Mean score for AI detections</div>
                </div>
                
                <div class="metric-card">
                    <h3>Avg Confidence (Human)</h3>
                    <div class="metric-value">{avg_confidence_human:.2f}</div>
                    <div class="metric-subtitle">Mean score for human detections</div>
                </div>
            </div>
            
            <div class="recent-activity">
                <h3>Recent Activity</h3>
                {recent_rows}
            </div>
        </div>
    </body>
    </html>
    '''
    
    return dashboard_html

@app.route('/log', methods=['GET'])
def view_log():
    """
    View the most recent audit log entries.
    """
    try:
        entries = get_log_entries(limit=50)
        return jsonify({
            'entries': entries,
            'count': len(entries)
        }), 200
    except Exception as e:
        return jsonify({
            'error': f'Error retrieving log: {str(e)}'
        }), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Simple health check endpoint."""
    return jsonify({'status': 'healthy'}), 200

@app.errorhandler(429)
def ratelimit_error(e):
    """Handle rate limit exceeded errors."""
    return jsonify({
        'error': 'Rate limit exceeded. Please slow down your submissions.',
        'retry_after': '60 seconds'
    }), 429

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)