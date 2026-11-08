
import streamlit as st
import re
import html
import requests
import sqlite3
import hashlib
import secrets
from datetime import datetime
from io import BytesIO

import speech_recognition as sr
from streamlit_mic_recorder import mic_recorder
from spellchecker import SpellChecker
import language_tool_python
from gtts import gTTS

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import A4


# ============== PAGE CONFIG ==============

st.set_page_config(
    page_title="Autocorrect Tool",
    page_icon="📝",
    layout="wide"
)


# ============== LOGIN & REGISTRATION ==============

DB_NAME = "users.db"


def init_user_database():
    """Create a local SQLite database for registered users."""
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()


def hash_password(password, salt=None):
    """Hash passwords with PBKDF2-HMAC-SHA256."""
    if salt is None:
        salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        200_000
    )
    return salt.hex() + "$" + password_hash.hex()


def verify_password(password, stored_value):
    try:
        salt_hex, expected_hash = stored_value.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        actual_hash = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            200_000
        ).hex()
        return secrets.compare_digest(actual_hash, expected_hash)
    except (ValueError, TypeError):
        return False


init_user_database()

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False
if "auth_username" not in st.session_state:
    st.session_state["auth_username"] = ""

if not st.session_state["authenticated"]:
    st.title("🔐 Autocorrect Tool - Account")
    st.write("Login to use the Autocorrect Tool, or create a new account.")

    login_tab, register_tab = st.tabs(["🔑 Login", "📝 Register"])

    with login_tab:
        with st.form("login_form"):
            login_username = st.text_input("Username")
            login_password = st.text_input("Password", type="password")
            login_submit = st.form_submit_button(
                "Login",
                use_container_width=True
            )

        if login_submit:
            username = login_username.strip()
            with sqlite3.connect(DB_NAME) as conn:
                row = conn.execute(
                    "SELECT username, password_hash FROM users WHERE username = ?",
                    (username,)
                ).fetchone()

            if row and verify_password(login_password, row[1]):
                st.session_state["authenticated"] = True
                st.session_state["auth_username"] = row[0]
                st.success("Login successful!")
                st.rerun()
            else:
                st.error("Incorrect username or password.")

    with register_tab:
        with st.form("register_form"):
            new_username = st.text_input("Choose a username")
            new_password = st.text_input(
                "Create password",
                type="password"
            )
            confirm_password = st.text_input(
                "Confirm password",
                type="password"
            )
            register_submit = st.form_submit_button(
                "Create Account",
                use_container_width=True
            )

        if register_submit:
            username = new_username.strip()

            if len(username) < 3:
                st.warning("Username must contain at least 3 characters.")
            elif len(new_password) < 8:
                st.warning("Password must contain at least 8 characters.")
            elif new_password != confirm_password:
                st.error("Passwords do not match.")
            else:
                try:
                    password_value = hash_password(new_password)
                    with sqlite3.connect(DB_NAME) as conn:
                        conn.execute(
                            """
                            INSERT INTO users (username, password_hash, created_at)
                            VALUES (?, ?, ?)
                            """,
                            (
                                username,
                                password_value,
                                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            )
                        )
                        conn.commit()

                    st.session_state["authenticated"] = True
                    st.session_state["auth_username"] = username
                    st.success("Account created successfully!")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("This username is already registered. Please choose another.")

    st.stop()

st.sidebar.success(f"Logged in as: {st.session_state['auth_username']}")
if st.sidebar.button("🚪 Logout", use_container_width=True):
    st.session_state["authenticated"] = False
    st.session_state["auth_username"] = ""
    st.rerun()



# ============== SESSION STATE ==============

defaults = {
    "user_input": "",
    "corrected_text": "",
    "grammar_corrected_text": "",
    "history": [],
    "suggestions": [],
    "grammar_results": [],
    "recorded_audio": None,
    "tts_audio": None,
    "theme_mode": "Light",
    "translation_input": ""
}

for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value


# ============== SPELL CHECKER ==============

spell = SpellChecker()

common_words = {
    "helo": "hello",
    "helllo": "hello",
    "frend": "friend",
    "freind": "friend",
    "yu": "you",
    "thnks": "thanks",
    "thier": "their",
    "programing": "programming",
    "pyhton": "python",
    "langauge": "language",
    "becuse": "because",
    "recieve": "receive",
    "teh": "the",
    "writting": "writing",
    "studing": "studying",
    "managment": "management"
}


def correct_spelling(text):

    words = re.findall(r"\b[\w']+\b|\W+", text)
    corrected = []
    suggestions = []

    for word in words:

        if not re.fullmatch(r"[\w']+", word):
            corrected.append(word)
            continue

        lower_word = word.lower()

        if lower_word in common_words:
            new_word = common_words[lower_word]

        elif lower_word not in spell:
            new_word = spell.correction(lower_word) or lower_word

        else:
            new_word = lower_word

        if new_word != lower_word:
            suggestions.append(f"{word} → {new_word}")

        if word.istitle():
            new_word = new_word.capitalize()
        elif word.isupper():
            new_word = new_word.upper()

        corrected.append(new_word)

    return "".join(corrected), suggestions


# ============== GRAMMAR TOOL ==============

@st.cache_resource
def get_grammar_tool():
    return language_tool_python.LanguageTool("en-US")


# ============== HISTORY ==============

def add_history(original, corrected):

    st.session_state.history.insert(0, {
        "time": datetime.now().strftime("%d-%m-%Y %I:%M %p"),
        "original": original,
        "corrected": corrected
    })


# ============== THEME ==============

st.sidebar.title("⚙️ Settings")

theme = st.sidebar.selectbox(
    "🎨 Choose Theme",
    ["Light", "Dark"],
    key="theme_mode"
)

if theme == "Dark":

    bg = "#171421"
    card = "#292437"
    color = "#FFFFFF"
    sidebar = "#211c30"

else:

    bg = "#F7F5FC"
    card = "#FFFFFF"
    color = "#29213A"
    sidebar = "#EEE8F7"


st.markdown(f"""
<style>
.stApp {{
    background-color: {bg};
    color: {color};
}}

[data-testid="stSidebar"] {{
    background-color: {sidebar};
}}

[data-testid="stHeader"] {{
    background-color: {bg};
}}

[data-testid="stToolbar"] {{
    background-color: {bg};
}}

.stTextArea textarea {{
    background-color: {card};
    color: {color};
    border-radius: 12px;
}}

div.stButton > button {{
    background-color: #7545A6;
    color: white;
    border-radius: 10px;
    border: none;
    padding: 9px 18px;
}}

div.stButton > button:hover {{
    background-color: #5D328B;
    color: white;
}}
</style>
""", unsafe_allow_html=True)


# ============== HEADER ==============

st.title("📝 Autocorrect Tool Using Python")

st.write(
    "Correct spelling and check grammar mistakes easily!"
)

st.caption(
    "Developed using Python, Streamlit, PySpellChecker and LanguageTool."
)

# ============== PROFESSIONAL USER DASHBOARD ==============

username = st.session_state.get("auth_username", "User")
with sqlite3.connect(DB_NAME) as conn:
    profile_row = conn.execute(
        "SELECT created_at FROM users WHERE username = ?",
        (username,)
    ).fetchone()

joined_text = profile_row[0] if profile_row else "Not available"
activity_count = len(st.session_state.history)
current_word_count = len(st.session_state.corrected_text.split())
correction_count = len(st.session_state.suggestions)

st.markdown("## 👋 Welcome to Your Dashboard")
st.write(f"Hello **{username}**! Manage your writing and explore your tools from here.")

profile_col, activity_col = st.columns([1, 2])
with profile_col:
    with st.container(border=True):
        st.markdown("### 👤 User Profile")
        st.write(f"**Username:** {username}")
        st.caption(f"**Account created:** {joined_text}")
with activity_col:
    metric1, metric2, metric3 = st.columns(3)
    metric1.metric("Activity Entries", activity_count)
    metric2.metric("Words in Current Result", current_word_count)
    metric3.metric("Spelling Fixes", correction_count)

with st.container(border=True):
    st.markdown("### 🚀 Your Tools")
    feature1, feature2, feature3 = st.columns(3)
    feature1.markdown("**✍️ Spelling & Grammar**\n\nCorrect spelling and check English grammar.")
    feature2.markdown("**🎤 Voice & Audio**\n\nConvert speech to text and listen to corrected text.")
    feature3.markdown("**🌍 Translation & Reports**\n\nTranslate text and download text or PDF reports.")

st.divider()


# ============== VOICE INPUT ==============

st.subheader("🎤 Voice Input")

st.write("Click the microphone, speak and stop recording.")

audio = mic_recorder(
    start_prompt="🎤 Start Recording",
    stop_prompt="⏹️ Stop Recording",
    just_once=True,
    use_container_width=True,
    key="voice_recorder",
    format="wav"
)

if audio and audio.get("bytes"):
    st.session_state["recorded_audio"] = audio["bytes"]


if st.session_state["recorded_audio"]:

    st.success("Voice recording completed!")

    if st.button("🔊 Convert Voice to Text"):

        try:

            recognizer = sr.Recognizer()

            audio_file = BytesIO(
                st.session_state["recorded_audio"]
            )

            with sr.AudioFile(audio_file) as source:
                audio_data = recognizer.record(source)

            with st.spinner("Converting voice to text..."):

                voice_text = recognizer.recognize_google(
                    audio_data,
                    language="en-IN"
                )

            st.session_state["user_input"] = voice_text
            st.session_state["recorded_audio"] = None

            st.success("Voice converted successfully!")

            st.rerun()

        except sr.UnknownValueError:
            st.error("Voice not understood. Please speak clearly.")

        except sr.RequestError as e:
            st.error(f"Speech Recognition service error: {e}")

        except Exception as e:
            st.error(f"Voice Error: {e}")


st.divider()


# ============== TEXT CORRECTION ==============

st.subheader("📝 Text Correction")

user_text = st.text_area(
    "Enter your text here:",
    height=150,
    key="user_input",
    placeholder="Example: Helo frend how are yu"
)

col1, col2 = st.columns(2)

with col1:

    correct_button = st.button(
        "✨ Correct Spelling",
        use_container_width=True
    )

with col2:

    clear_button = st.button(
        "🗑️ Clear Text",
        use_container_width=True
    )


if clear_button:

    st.session_state["user_input"] = ""
    st.session_state["corrected_text"] = ""
    st.session_state["grammar_corrected_text"] = ""
    st.session_state["suggestions"] = []
    st.session_state["grammar_results"] = []
    st.session_state["recorded_audio"] = None
    st.session_state["tts_audio"] = None

    st.rerun()


if correct_button:

    if user_text.strip():

        corrected, suggestions = correct_spelling(user_text)

        st.session_state["corrected_text"] = corrected
        st.session_state["suggestions"] = suggestions
        st.session_state["grammar_corrected_text"] = ""
        st.session_state["grammar_results"] = []
        st.session_state["tts_audio"] = None

        add_history(user_text, corrected)

    else:

        st.warning("Please enter some text first.")


# ============== DASHBOARD ==============

if st.session_state.corrected_text:

    original = user_text
    corrected = st.session_state.corrected_text

    st.divider()
    st.subheader("📊 Correction Dashboard")

    c1, c2, c3 = st.columns(3)

    c1.metric("Original Words", len(original.split()))

    c2.metric("Corrected Words", len(corrected.split()))

    c3.metric(
        "Corrections Made",
        len(st.session_state.suggestions)
    )

    st.subheader("📄 Original Text")
    st.write(original)

    st.subheader("✅ Corrected Text")
    st.success(corrected)

    st.subheader("💡 Spelling Suggestions")

    if st.session_state.suggestions:

        for suggestion in st.session_state.suggestions:
            st.write("•", suggestion)

    else:

        st.info("No spelling mistakes found!")

    st.subheader("📋 Copy Corrected Text")

    st.code(corrected, language="text")

    st.caption("Select the text and press Ctrl + C to copy.")

    st.download_button(
        "⬇️ Download Corrected Text",
        data=corrected,
        file_name="corrected_text.txt",
        mime="text/plain"
    )

    st.divider()


    # ============== GRAMMAR CHECKER ==============

    st.subheader("✍️ Grammar Checker")

    if st.button("🔍 Check Grammar"):

        try:

            with st.spinner("Checking grammar..."):

                grammar_tool = get_grammar_tool()
                matches = grammar_tool.check(corrected)

            st.session_state.grammar_results = [
                {
                    "message": item.message,
                    "context": item.context,
                    "suggestions": list(item.replacements)
                }
                for item in matches
            ]

            if not matches:

                st.success("No grammar issues found!")

            else:

                st.warning(
                    f"Grammar issues found: {len(matches)}"
                )

        except Exception as e:

            st.error(f"Grammar Error: {e}")


    for index, item in enumerate(
        st.session_state.grammar_results, 1
    ):

        st.write(f"**Issue {index}:** {item['message']}")

        st.write("Context:", item["context"])

        if item["suggestions"]:

            st.write(
                "Suggestions:",
                ", ".join(item["suggestions"])
            )


    if st.session_state.grammar_results:

        report = "GRAMMAR REPORT\n\n"

        for index, item in enumerate(
            st.session_state.grammar_results, 1
        ):

            report += f"Issue {index}: {item['message']}\n"

            report += f"Context: {item['context']}\n"

            report += (
                "Suggestions: "
                + ", ".join(item["suggestions"])
                + "\n\n"
            )

        st.download_button(
            "⬇️ Download Grammar Report",
            data=report,
            file_name="grammar_report.txt",
            mime="text/plain"
        )


    # ============== AUTO GRAMMAR CORRECTION ==============

    st.subheader("✨ Auto Grammar Correction")

    if st.button("⚡ Auto Correct Grammar"):

        try:

            with st.spinner("Correcting grammar..."):

                grammar_tool = get_grammar_tool()

                grammar_corrected = grammar_tool.correct(corrected)

            st.session_state.grammar_corrected_text = grammar_corrected

            add_history(corrected, grammar_corrected)

        except Exception as e:

            st.error(f"Grammar Correction Error: {e}")


    if st.session_state.grammar_corrected_text:

        st.success("Automatically Corrected Text")

        st.write(st.session_state.grammar_corrected_text)


    # ============== TEXT EDITOR ==============

    st.divider()

    st.subheader("✏️ Text Editor")

    edited_text = st.text_area(
        "Edit your corrected text:",
        value=st.session_state.corrected_text,
        height=150,
        key="editor_area"
    )

    if st.button("💾 Save Edited Text"):

        st.session_state.corrected_text = edited_text

        st.session_state.grammar_corrected_text = ""

        st.session_state.grammar_results = []

        add_history("Edited Text", edited_text)

        st.success("Edited text saved!")


    st.download_button(
        "⬇️ Download Edited Text",
        data=edited_text,
        file_name="edited_text.txt",
        mime="text/plain"
    )


    # ============== PDF REPORT ==============

    st.divider()

    st.subheader("📄 PDF Report Download")

    def create_pdf():

        buffer = BytesIO()

        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4
        )

        styles = getSampleStyleSheet()

        content = []

        content.append(
            Paragraph(
                "Autocorrect Tool Report",
                styles["Title"]
            )
        )

        content.append(Spacer(1, 20))

        content.append(
            Paragraph("Original Text", styles["Heading2"])
        )

        content.append(
            Paragraph(
                html.escape(original).replace("\n", "<br/>"),
                styles["BodyText"]
            )
        )

        content.append(Spacer(1, 15))

        content.append(
            Paragraph("Corrected Text", styles["Heading2"])
        )

        content.append(
            Paragraph(
                html.escape(corrected).replace("\n", "<br/>"),
                styles["BodyText"]
            )
        )

        content.append(Spacer(1, 15))

        content.append(
            Paragraph(
                "Spelling Corrections",
                styles["Heading2"]
            )
        )

        if st.session_state.suggestions:

            for item in st.session_state.suggestions:

                content.append(
                    Paragraph(
                        html.escape(item),
                        styles["BodyText"]
                    )
                )

        else:

            content.append(
                Paragraph(
                    "No spelling corrections.",
                    styles["BodyText"]
                )
            )

        content.append(Spacer(1, 15))

        content.append(
            Paragraph(
                "Grammar Results",
                styles["Heading2"]
            )
        )

        if st.session_state.grammar_results:

            for item in st.session_state.grammar_results:

                content.append(
                    Paragraph(
                        html.escape(item["message"]),
                        styles["BodyText"]
                    )
                )

        else:

            content.append(
                Paragraph(
                    "No grammar results available.",
                    styles["BodyText"]
                )
            )

        if st.session_state.grammar_corrected_text:

            content.append(Spacer(1, 15))

            content.append(
                Paragraph(
                    "Auto Grammar Corrected Text",
                    styles["Heading2"]
                )
            )

            content.append(
                Paragraph(
                    html.escape(
                        st.session_state.grammar_corrected_text
                    ),
                    styles["BodyText"]
                )
            )

        content.append(Spacer(1, 20))

        content.append(
            Paragraph(
                "Generated: "
                + datetime.now().strftime("%d-%m-%Y %I:%M %p"),
                styles["Italic"]
            )
        )

        doc.build(content)

        buffer.seek(0)

        return buffer.getvalue()


    st.download_button(
        "📥 Download PDF Report",
        data=create_pdf(),
        file_name="Autocorrect_Project_Report.pdf",
        mime="application/pdf"
    )


    # ============== TEXT TO SPEECH ==============

    st.divider()

    st.subheader("🔊 Text-to-Speech")

    st.write("Listen to your corrected text.")

    speech_text = (
        st.session_state.grammar_corrected_text
        or st.session_state.corrected_text
    )

    if st.button("🔊 Listen to Corrected Text"):

        try:

            with st.spinner("Creating audio..."):

                tts = gTTS(
                    text=speech_text,
                    lang="en",
                    slow=False
                )

                audio_buffer = BytesIO()

                tts.write_to_fp(audio_buffer)

                audio_buffer.seek(0)

                st.session_state["tts_audio"] = audio_buffer.getvalue()

        except Exception as e:

            st.error(f"Text-to-Speech Error: {e}")


    if st.session_state.get("tts_audio"):

        st.audio(
            st.session_state["tts_audio"],
            format="audio/mp3"
        )

        st.download_button(
            "⬇️ Download Speech Audio",
            data=st.session_state["tts_audio"],
            file_name="corrected_speech.mp3",
            mime="audio/mpeg"
        )


# ============== MARATHI & HINDI TRANSLATOR ==============

st.divider()

st.subheader("🌍 Marathi & Hindi Language Support")

st.write(
    "Translate text into English, Marathi or Hindi."
)

translation_text = st.text_area(
    "Enter text to translate:",
    height=120,
    key="translation_input",
    placeholder="Enter English, Marathi or Hindi text..."
)

source_language = st.selectbox(
    "Select Source Language",
    ["Auto Detect", "English", "Marathi", "Hindi"]
)

target_language = st.selectbox(
    "Translate To",
    ["Marathi", "Hindi", "English"]
)

language_codes = {
    "Auto Detect": "autodetect",
    "English": "en",
    "Marathi": "mr",
    "Hindi": "hi"
}

if st.button("🌐 Translate Text"):

    if translation_text.strip():

        try:

            with st.spinner("Translating text..."):

                source_code = language_codes[source_language]

                target_code = language_codes[target_language]

                lang_pair = f"{source_code}|{target_code}"

                response = requests.get(
                    "https://api.mymemory.translated.net/get",
                    params={
                        "q": translation_text,
                        "langpair": lang_pair
                    },
                    timeout=20
                )

                response.raise_for_status()

                result = response.json()

                if str(result.get("responseStatus")) == "200":

                    translated_text = html.unescape(
                        result["responseData"]["translatedText"]
                    )

                    st.success("Translation Completed!")

                    st.subheader("✅ Translated Text")

                    st.write(translated_text)

                    st.code(translated_text, language="text")

                    st.download_button(
                        "⬇️ Download Translation",
                        data=translated_text,
                        file_name="translated_text.txt",
                        mime="text/plain"
                    )

                else:

                    st.error(
                        result.get(
                            "responseDetails",
                            "Translation could not be completed."
                        )
                    )

        except requests.exceptions.Timeout:

            st.error("Translation request timed out. Please try again.")

        except requests.exceptions.RequestException as e:

            st.error(f"Network Error: {e}")

        except Exception as e:

            st.error(f"Translation Error: {e}")

    else:

        st.warning("Please enter text to translate.")


# ============== CORRECTION HISTORY ==============

st.divider()

st.subheader("📚 Correction History")

if st.session_state.history:

    for item in st.session_state.history:

        with st.expander("🕒 " + item["time"]):

            st.write("**Original:**", item["original"])

            st.write("**Corrected:**", item["corrected"])


    if st.button("🗑️ Clear History"):

        st.session_state.history = []

        st.rerun()

else:

    st.info("No correction history available yet.")


# ============== FOOTER ==============

st.divider()

st.caption(
    "💜 Autocorrect Tool Using Python | Developed with Streamlit"
)
