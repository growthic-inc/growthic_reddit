import os
import logging
import time
import json
from typing import List, Dict, Any, Optional
import streamlit as st
import praw
import prawcore
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, auth
import requests
import praw.exceptions as praw_ex

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Page config
st.set_page_config(
    page_title="Reddit Multi-Account Poster",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="expanded"
)

class FirebaseAuth:
    """Handle Firebase authentication."""
    
    def __init__(self):
        self.initialized = self._initialize_firebase()
        self.web_api_key = os.getenv("FIREBASE_WEB_API_KEY")
    
    def _initialize_firebase(self):
        """Initialize Firebase Admin SDK."""
        try:
            import streamlit as st  # only available on Streamlit Cloud

            # Check if already initialized
            if firebase_admin._apps:
                return True

            firebase_credentials_path = os.getenv("FIREBASE_CREDENTIALS_PATH")

            # Case 1: Use Streamlit secrets (Streamlit Cloud)
            if "firebase" in st.secrets:
                cred = credentials.Certificate(dict(st.secrets["firebase"]))

            # Case 2: Use local firebase-service-account.json if available
            elif not firebase_credentials_path:
                default_path = os.path.join(os.path.dirname(__file__), "firebase-service-account.json")
                if os.path.exists(default_path):
                    firebase_credentials_path = default_path

            if firebase_credentials_path and os.path.exists(firebase_credentials_path):
                cred = credentials.Certificate(firebase_credentials_path)
            else:
                cred = credentials.ApplicationDefault()

            # Initialize
            firebase_admin.initialize_app(cred)
            logger.info("Firebase Admin SDK initialized successfully")
            return True

        except Exception as e:
            logger.exception(f"Firebase initialization failed: {e}")
            return False

    
    def authenticate_user(self, email: str, password: str) -> Dict[str, Any]:
        """Authenticate user with email and password using Firebase REST API."""
        if not self.initialized or not self.web_api_key:
            return {
                "success": False,
                "error": "Firebase not properly configured"
            }
        
        try:
            # Firebase REST API endpoint for sign in
            url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={self.web_api_key}"
            
            payload = {
                "email": email,
                "password": password,
                "returnSecureToken": True
            }
            
            response = requests.post(url, json=payload)
            data = response.json()
            
            if response.status_code == 200:
                # Verify the ID token with Admin SDK
                id_token = data.get("idToken")
                try:
                    decoded_token = auth.verify_id_token(id_token)
                    return {
                        "success": True,
                        "user": {
                            "uid": decoded_token["uid"],
                            "email": decoded_token.get("email"),
                            "name": decoded_token.get("name") or decoded_token.get("email", "").split("@")[0]
                        }
                    }
                except Exception as e:
                    return {
                        "success": False,
                        "error": f"Token verification failed: {str(e)}"
                    }
            else:
                error_message = data.get("error", {}).get("message", "Authentication failed")
                
                # Make error messages more user-friendly
                if "EMAIL_NOT_FOUND" in error_message:
                    error_message = "No user found with this email address"
                elif "INVALID_PASSWORD" in error_message:
                    error_message = "Invalid password"
                elif "USER_DISABLED" in error_message:
                    error_message = "User account has been disabled"
                elif "TOO_MANY_ATTEMPTS_TRY_LATER" in error_message:
                    error_message = "Too many failed attempts. Please try again later"
                
                return {
                    "success": False,
                    "error": error_message
                }
                
        except requests.exceptions.RequestException as e:
            return {
                "success": False,
                "error": f"Network error: {str(e)}"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Authentication error: {str(e)}"
            }

class RedditCore:
    """Core Reddit functionality for multi-account posting."""

    def __init__(self):
        self.reddit_accounts: Dict[str, praw.Reddit] = {}
        self.account_usernames: List[str] = []
        self.is_loaded = False

    def load_accounts_from_env(self) -> Dict[str, Dict[str, str]]:
        """Load up to 30 Reddit accounts from environment variables."""
        accounts = {}
        
        # Try to load up to 30 accounts
        for i in range(1, 31):
            prefix = f"REDDIT_ACCOUNT_{i}"
            config = {
                "client_id": os.getenv(f"{prefix}_CLIENT_ID"),
                "client_secret": os.getenv(f"{prefix}_CLIENT_SECRET"),
                "username": os.getenv(f"{prefix}_USERNAME"),
                "password": os.getenv(f"{prefix}_PASSWORD"),
                "user_agent": os.getenv(f"{prefix}_USER_AGENT"),
            }
            
            # Check if all required fields are present
            if all(config.values()):
                account_name = config["username"]
                accounts[account_name] = config
                logger.info(f"Found account config: {account_name}")
            elif any(config.values()):
                logger.warning(f"Incomplete configuration for {prefix} - skipping")
        
        # Fallback to single account format
        if not accounts:
            config = {
                "client_id": os.getenv("REDDIT_CLIENT_ID"),
                "client_secret": os.getenv("REDDIT_CLIENT_SECRET"),
                "username": os.getenv("REDDIT_USERNAME"),
                "password": os.getenv("REDDIT_PASSWORD"),
                "user_agent": os.getenv("REDDIT_USER_AGENT"),
            }
            if all(config.values()):
                accounts[config["username"]] = config
        
        logger.info(f"Total accounts found: {len(accounts)}")
        return accounts

    def load_accounts(self) -> Dict[str, Any]:
        """Load and configure all available Reddit accounts."""
        try:
            env_accounts = self.load_accounts_from_env()
            if not env_accounts:
                return {
                    "success": False,
                    "error": "No Reddit accounts found in environment variables",
                    "accounts": [],
                    "total_accounts": 0
                }
            
            success_count = 0
            errors = []
            loaded_accounts = []
            
            for username, config in env_accounts.items():
                try:
                    reddit_client = praw.Reddit(
                        client_id=config["client_id"],
                        client_secret=config["client_secret"],
                        user_agent=config["user_agent"],
                        username=config["username"],
                        password=config["password"],
                    )
                    
                    # Test the connection
                    user = reddit_client.user.me()
                    if user.name:
                        self.reddit_accounts[username] = reddit_client
                        self.account_usernames.append(username)
                        loaded_accounts.append({
                            "id": success_count + 1,
                            "username": username
                        })
                        success_count += 1
                        logger.info(f"Successfully loaded account: {username}")
                    
                except Exception as e:
                    error_msg = f"Failed to load {username}: {str(e)}"
                    errors.append(error_msg)
                    logger.error(error_msg)
            
            if success_count == 0:
                return {
                    "success": False,
                    "error": "No accounts could be loaded",
                    "details": errors,
                    "accounts": [],
                    "total_accounts": 0
                }
            
            self.is_loaded = True
            return {
                "success": True,
                "message": f"Successfully loaded {success_count} Reddit account(s)",
                "accounts": loaded_accounts,
                "total_accounts": success_count,
                "errors": errors if errors else None
            }
            
        except Exception as e:
            logger.error(f"Account loading failed: {str(e)}")
            return {
                "success": False,
                "error": f"Account loading failed: {str(e)}",
                "accounts": [],
                "total_accounts": 0
            }

    def get_reddit_client(self, account_id: int) -> Optional[praw.Reddit]:
        """Get Reddit client by account ID (1-based)."""
        if not self.is_loaded or account_id < 1 or account_id > len(self.account_usernames):
            return None
        
        username = self.account_usernames[account_id - 1]
        return self.reddit_accounts.get(username)

    def get_account_username(self, account_id: int) -> Optional[str]:
        """Get account username by ID."""
        if not self.is_loaded or account_id < 1 or account_id > len(self.account_usernames):
            return None
        return self.account_usernames[account_id - 1]

    def verify_subreddit(self, subreddit_name: str, account_id: int = 1) -> Dict[str, Any]:
        """Verify if a subreddit exists and is accessible."""
        if not subreddit_name:
            return {"success": False, "error": "subreddit_name is required"}
        
        reddit_client = self.get_reddit_client(account_id)
        if not reddit_client:
            return {
                "success": False, 
                "error": f"Invalid account_id: {account_id}. Use 1-{len(self.account_usernames)}"
            }
        
        try:
            subreddit = reddit_client.subreddit(subreddit_name)

            # Safer: wrap API attribute fetch
            try:
                display_name = subreddit.display_name
                subscribers = getattr(subreddit, "subscribers", 0) or 0
                description = (getattr(subreddit, "description", "") or "")[:200]
                over18 = bool(getattr(subreddit, "over18", False))
            except Exception:
                # If restricted/private, still return minimal info
                return {
                    "success": False,
                    "error": f"Subreddit r/{subreddit_name} could not be accessed (may be private/restricted)"
                }

            return {
                "success": True,
                "subreddit": {
                    "name": subreddit_name.lower(),
                    "display_name": display_name,
                    "subscribers": subscribers,
                    "description": description,
                    "nsfw": over18,
                    "exists": True
                }
            }
        
        except (prawcore.exceptions.Redirect, prawcore.exceptions.NotFound):
            return {"success": False, "error": f"Subreddit r/{subreddit_name} not found"}
        except prawcore.exceptions.Forbidden:
            return {"success": False, "error": f"Subreddit r/{subreddit_name} is private or restricted"}
        except Exception as e:
            return {"success": False, "error": f"Failed to verify subreddit: {str(e)}"}

    def get_flairs(self, subreddit_name: str, account_id: int) -> Dict[str, Any]:
        """Get available post flairs for a subreddit."""
        if not subreddit_name:
            return {"success": False, "error": "subreddit_name is required"}
        
        reddit_client = self.get_reddit_client(account_id)
        if not reddit_client:
            return {
                "success": False,
                "error": f"Invalid account_id: {account_id}. Use 1-{len(self.account_usernames)}"
            }
        
        try:
            subreddit = reddit_client.subreddit(subreddit_name)
            templates = list(subreddit.flair.link_templates)
            
            flairs = []
            for template in templates:
                flair_data = {
                    "id": template.get("id"),
                    "text": template.get("text"),
                    "text_color": template.get("text_color"),
                    "background_color": template.get("background_color"),
                    "text_editable": template.get("text_editable", False)
                }
                flairs.append(flair_data)
            
            return {
                "success": True,
                "subreddit": subreddit_name,
                "flair_count": len(flairs),
                "flairs": flairs
            }
            
        except (prawcore.exceptions.Redirect, prawcore.exceptions.NotFound):
            return {"success": False, "error": f"Subreddit r/{subreddit_name} not found"}
        except prawcore.exceptions.Forbidden:
            return {"success": False, "error": f"Cannot access flairs for r/{subreddit_name} - may be private"}
        except Exception as e:
            return {"success": False, "error": f"Failed to get flairs: {str(e)}"}

    def post_content(self, post_data: Dict[str, Any]) -> Dict[str, Any]:
        """Post content to a subreddit using specified account."""
        # Extract required parameters
        account_id = post_data.get("account_id")
        subreddit_name = post_data.get("subreddit_name")
        title = post_data.get("title")
        
        if not all([account_id, subreddit_name, title]):
            return {
                "success": False,
                "error": "account_id, subreddit_name, and title are required"
            }
        
        reddit_client = self.get_reddit_client(account_id)
        if not reddit_client:
            return {
                "success": False,
                "error": f"Invalid account_id: {account_id}. Use 1-{len(self.account_usernames)}"
            }
        
        # Content parameters
        body = post_data.get("body")
        url = post_data.get("url")
        image_path = post_data.get("image_path")
        
        # Posting options
        flair_id = post_data.get("flair_id")
        flair_text = post_data.get("flair_text")
        nsfw = bool(post_data.get("nsfw", False))
        spoiler = bool(post_data.get("spoiler", False))
        send_replies = bool(post_data.get("send_replies", True))
        
        # Validate content type
        content_types = [1 if body else 0, 1 if url else 0, 1 if image_path else 0]
        if sum(content_types) > 1:
            return {
                "success": False,
                "error": "Provide only ONE content type: body (text), url (link), or image_path (image)"
            }
        
        username = self.get_account_username(account_id)
        
        try:
            subreddit = reddit_client.subreddit(subreddit_name)
            submission = None
            post_type = ""
            
            # Submit based on content type
            if image_path:
                if not os.path.isfile(image_path):
                    return {"success": False, "error": f"Image file not found: {image_path}"}
                
                submission = subreddit.submit_image(
                    title=title,
                    image_path=image_path,
                    flair_id=flair_id,
                    flair_text=flair_text,
                    nsfw=nsfw,
                    spoiler=spoiler,
                    send_replies=send_replies
                )
                post_type = "image"
                
            elif url and not body:
                submission = subreddit.submit(
                    title=title,
                    url=url,
                    flair_id=flair_id,
                    flair_text=flair_text,
                    nsfw=nsfw,
                    spoiler=spoiler,
                    send_replies=send_replies,
                    resubmit=True
                )
                post_type = "link"
                
            else:  # Text post (body can be empty)
                submission = subreddit.submit(
                    title=title,
                    selftext=body or "",
                    flair_id=flair_id,
                    flair_text=flair_text,
                    nsfw=nsfw,
                    spoiler=spoiler,
                    send_replies=send_replies
                )
                post_type = "text"
            
            # Prepare response
            result = {
                "success": True,
                "message": f"Successfully posted to r/{subreddit_name}",
                "post_details": {
                    "post_id": submission.id if submission else None,
                    "post_url": f"https://reddit.com{submission.permalink}" if submission else None,
                    "post_type": post_type,
                    "title": title,
                    "subreddit": subreddit_name,
                    "account_used": username,
                    "flair_applied": bool(flair_id or flair_text),
                    "nsfw": nsfw,
                    "spoiler": spoiler
                }
            }
            
            logger.info(f"Posted successfully: {title[:50]}... to r/{subreddit_name} using {username}")
            return result
            
        except prawcore.exceptions.Forbidden as e:
            error_msg = f"Forbidden: Account '{username}' cannot post to r/{subreddit_name}"
            logger.error(f"{error_msg}: {e}")
            return {"success": False, "error": error_msg, "details": str(e)}
            
        except prawcore.exceptions.TooLarge as e:
            return {"success": False, "error": f"Content too large: {str(e)}"}
            
        except praw_ex.InvalidFlairTemplateID as e:
            return {"success": False, "error": f"Invalid flair ID: {str(e)}"}   

        except praw_ex.RedditAPIException as e:
            for it in getattr(e, "items", []):
                if getattr(it, "error_type", "") == "INVALID_FLAIR_TEMPLATE_ID":
                    return {"success": False, "error": "Invalid flair ID"}
            # fall through for other API errors
            raise         
        
        except Exception as e:
            error_msg = f"Failed to post: {str(e)}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg}

# Initialize components
@st.cache_resource
def init_components():
    """Initialize Firebase and Reddit components."""
    firebase_auth = FirebaseAuth()
    reddit_core = RedditCore()
    return firebase_auth, reddit_core

def render_login_page(firebase_auth):
    """Render the login page with email/password form."""
    st.title("🚀 Reddit Multi-Account Poster")
    st.markdown("---")
    
    col1, col2, col3 = st.columns([1, 2, 1])
    
    with col2:
        st.markdown("### Welcome! Please sign in to continue")
        
        if firebase_auth.initialized:
            st.info("🔒 Firebase authentication is enabled")
            
            # Login form
            with st.form("login_form"):
                email = st.text_input("Email", placeholder="your@email.com")
                password = st.text_input("Password", type="password", placeholder="Enter your password")
                
                submitted = st.form_submit_button("🔓 Sign In", type="primary")
                
                if submitted:
                    if not email or not password:
                        st.error("Please enter both email and password")
                    else:
                        with st.spinner("Authenticating..."):
                            result = firebase_auth.authenticate_user(email, password)
                        
                        if result["success"]:
                            st.session_state.authenticated = True
                            st.session_state.user = result["user"]
                            st.success("✅ Login successful!")
                            time.sleep(1)  # Brief delay to show success message
                            st.rerun()
                        else:
                            st.error(f"❌ Login failed: {result['error']}")
            
            st.markdown("---")
            st.markdown("**Need an account?** Contact your administrator to set up Firebase Authentication.")
            
        else:
            st.warning("🔒 Firebase not configured - Running in demo mode")
            if st.button("Continue without Authentication"):
                st.session_state.authenticated = True
                st.session_state.user = {
                    "uid": "demo_user",
                    "email": "demo@example.com",
                    "name": "Demo User"
                }
                st.rerun()

def render_main_app(firebase_auth, reddit_core):
    """Render the main application."""
    # Sidebar
    with st.sidebar:
        st.title("🚀 Reddit Poster")
        
        # User info
        user = st.session_state.get("user", {})
        st.markdown(f"**Logged in as:** {user.get('name', 'Unknown')}")
        st.markdown(f"**Email:** {user.get('email', 'Unknown')}")
        
        if st.button("Sign Out"):
            st.session_state.authenticated = False
            st.session_state.user = None
            # Clear other session state
            for key in ['accounts_loaded', 'reddit_accounts']:
                if key in st.session_state:
                    del st.session_state[key]
            st.rerun()
        
        st.markdown("---")
        
        # Load Reddit accounts
        if st.button("🔥 Load Reddit Accounts"):
            with st.spinner("Loading Reddit accounts..."):
                result = reddit_core.load_accounts()
                if result["success"]:
                    st.success(f"✅ Loaded {result['total_accounts']} accounts")
                    st.session_state.accounts_loaded = True
                    st.session_state.reddit_accounts = result["accounts"]
                else:
                    st.error(f"❌ {result['error']}")
        
        # Account status
        if hasattr(st.session_state, 'accounts_loaded') and st.session_state.accounts_loaded:
            accounts = st.session_state.get('reddit_accounts', [])
            st.success(f"📊 {len(accounts)} accounts loaded")
            
            with st.expander("View Accounts"):
                for acc in accounts:
                    st.write(f"• {acc['username']}")
    
    # Main content
    st.title("Reddit Multi-Account Poster")
    
    if not hasattr(st.session_state, 'accounts_loaded') or not st.session_state.accounts_loaded:
        st.warning("⚠️ Please load Reddit accounts from the sidebar first")
        return
    
    accounts = st.session_state.get('reddit_accounts', [])
    if not accounts:
        st.error("❌ No Reddit accounts available")
        return
    
    # Tabs for different functionalities
    tab1, tab2, tab3 = st.tabs(["📝 Create Post", "🔍 Verify Subreddit", "🏷️ Get Flairs"])
    
    with tab1:
        render_post_tab(reddit_core, accounts)
    
    with tab2:
        render_verify_tab(reddit_core, accounts)
    
    with tab3:
        render_flair_tab(reddit_core, accounts)

def render_post_tab(reddit_core, accounts):
    """Render the post creation tab."""
    st.header("Create New Post")
    
    col1, col2 = st.columns([1, 1])
    
    with col1:
        # Account selection
        account_options = [f"{acc['id']}. {acc['username']}" for acc in accounts]
        selected_account = st.selectbox("Select Reddit Account", account_options)
        account_id = int(selected_account.split('.')[0]) if selected_account else 1
        
        # Subreddit
        subreddit_name = st.text_input("Subreddit Name", placeholder="e.g., AskReddit")
        
        # Title
        title = st.text_input("Post Title", placeholder="Enter your post title")
    
    with col2:
        # Post type
        post_type = st.radio("Post Type", ["Text Post", "Link Post", "Image Post"])
        
        # Content based on type
        if post_type == "Text Post":
            body = st.text_area("Post Content", placeholder="Enter your text content here...")
            url = None
            image_path = None
        elif post_type == "Link Post":
            url = st.text_input("URL", placeholder="https://example.com")
            body = None
            image_path = None
        else:  # Image Post
            uploaded_file = st.file_uploader("Choose an image", type=['png', 'jpg', 'jpeg'])
            if uploaded_file:
                # Save uploaded file
                os.makedirs("temp", exist_ok=True)
                image_path = f"temp/{uploaded_file.name}"
                with open(image_path, "wb") as f:
                    f.write(uploaded_file.getbuffer())
            else:
                image_path = None
            body = None
            url = None
    
    # Advanced options
    with st.expander("Advanced Options"):
        col3, col4 = st.columns(2)
        with col3:
            nsfw = st.checkbox("NSFW")
            spoiler = st.checkbox("Spoiler")
        with col4:
            send_replies = st.checkbox("Send Replies to Inbox", value=True)
            flair_text = st.text_input("Custom Flair Text (optional)")
            flair_id = st.text_input("Flair ID (optional)")

    # Submit button
    if st.button("🚀 Submit Post", type="primary"):
        if not all([subreddit_name, title]):
            st.error("Please fill in subreddit name and title")
            return
        
        post_data = {
            "account_id": account_id,
            "subreddit_name": subreddit_name,
            "title": title,
            "body": body,
            "url": url,
            "image_path": image_path,
            "flair_id": flair_id if flair_id else None,
            "flair_text": flair_text if flair_text else None,
            "nsfw": nsfw,
            "spoiler": spoiler,
            "send_replies": send_replies
        }
        
        with st.spinner("Posting to Reddit..."):
            result = reddit_core.post_content(post_data)
        
        if result["success"]:
            st.success("✅ Post submitted successfully!")
            post_details = result["post_details"]
            
            st.markdown("### Post Details")
            col5, col6 = st.columns(2)
            with col5:
                st.write(f"**Post ID:** {post_details['post_id']}")
                st.write(f"**Post Type:** {post_details['post_type']}")
                st.write(f"**Subreddit:** r/{post_details['subreddit']}")
            with col6:
                st.write(f"**Account Used:** {post_details['account_used']}")
                st.write(f"**NSFW:** {post_details['nsfw']}")
                st.write(f"**Spoiler:** {post_details['spoiler']}")
            
            if post_details['post_url']:
                st.markdown(f"**[View Post on Reddit]({post_details['post_url']})**")
        else:
            st.error(f"❌ Failed to post: {result['error']}")

def render_verify_tab(reddit_core, accounts):
    """Render the subreddit verification tab."""
    st.header("Verify Subreddit")
    
    col1, col2 = st.columns([1, 1])
    
    with col1:
        account_options = [f"{acc['id']}. {acc['username']}" for acc in accounts]
        selected_account = st.selectbox("Select Reddit Account", account_options, key="verify_account")
        account_id = int(selected_account.split('.')[0]) if selected_account else 1
        
        subreddit_name = st.text_input("Subreddit Name", placeholder="e.g., AskReddit", key="verify_subreddit")
        
        if st.button("🔍 Verify Subreddit"):
            if subreddit_name:
                with st.spinner("Verifying subreddit..."):
                    result = reddit_core.verify_subreddit(subreddit_name, account_id)
                
                if result["success"]:
                    subreddit_info = result["subreddit"]
                    st.success("✅ Subreddit found!")
                    
                    with col2:
                        st.markdown("### Subreddit Info")
                        st.write(f"**Name:** r/{subreddit_info['display_name']}")
                        st.write(f"**Subscribers:** {subreddit_info['subscribers']:,}")
                        st.write(f"**NSFW:** {subreddit_info['nsfw']}")
                        if subreddit_info['description']:
                            st.write(f"**Description:** {subreddit_info['description'][:100]}...")
                else:
                    st.error(f"❌ {result['error']}")
            else:
                st.error("Please enter a subreddit name")

def render_flair_tab(reddit_core, accounts):
    """Render the flair retrieval tab."""
    st.header("Get Subreddit Flairs")
    
    col1, col2 = st.columns([1, 1])
    
    with col1:
        account_options = [f"{acc['id']}. {acc['username']}" for acc in accounts]
        selected_account = st.selectbox("Select Reddit Account", account_options, key="flair_account")
        account_id = int(selected_account.split('.')[0]) if selected_account else 1
        
        subreddit_name = st.text_input("Subreddit Name", placeholder="e.g., AskReddit", key="flair_subreddit")
        
        if st.button("🏷️ Get Flairs"):
            if subreddit_name:
                with st.spinner("Fetching flairs..."):
                    result = reddit_core.get_flairs(subreddit_name, account_id)
                
                if result["success"]:
                    flairs = result["flairs"]
                    st.success(f"✅ Found {len(flairs)} flairs")
                    
                    with col2:
                        st.markdown("### Available Flairs")
                        if flairs:
                            for flair in flairs:
                                st.markdown(f"**{flair['text'] or 'No Text'}**")
                                st.write(f"ID: `{flair['id']}`")
                                if flair['text_editable']:
                                    st.write("✏️ Text Editable")
                                st.markdown("---")
                        else:
                            st.info("No flairs available for this subreddit")
                else:
                    st.error(f"❌ {result['error']}")
            else:
                st.error("Please enter a subreddit name")

def main():
    """Main application entry point."""
    # Initialize session state
    if 'authenticated' not in st.session_state:
        st.session_state.authenticated = False
    if 'user' not in st.session_state:
        st.session_state.user = None
    
    # Initialize components
    firebase_auth, reddit_core = init_components()
    
    # Route to appropriate page
    if not st.session_state.authenticated:
        render_login_page(firebase_auth)
    else:
        render_main_app(firebase_auth, reddit_core)

# Run the application
if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"Application error: {str(e)}")
        st.error("🚨 An error occurred while running the application")
        st.error(f"Error details: {str(e)}")
        st.info("Please check your configuration and try again")
