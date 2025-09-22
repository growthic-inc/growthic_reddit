import os
import logging
import time
import json
from typing import List, Dict, Any, Optional
import streamlit as st
import praw
import prawcore
import schedule
import threading
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, auth
import requests
import praw.exceptions as praw_ex
from datetime import datetime, timedelta

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
        self.web_api_key = self._get_web_api_key()
    
    def _get_web_api_key(self):
        """Get Firebase Web API key from environment variables or Streamlit secrets."""
        # First try environment variables
        web_api_key = os.getenv("FIREBASE_WEB_API_KEY")
        
        if not web_api_key:
            try:
                import streamlit as st
                # Try to get from Streamlit secrets (top level)
                if hasattr(st, 'secrets') and "FIREBASE_WEB_API_KEY" in st.secrets:
                    web_api_key = st.secrets["FIREBASE_WEB_API_KEY"]
            except Exception as e:
                logger.warning(f"Could not access Streamlit secrets for web_api_key: {e}")
        
        return web_api_key

    def _initialize_firebase(self):
        """Initialize Firebase Admin SDK."""
        try:
            import streamlit as st

            # Check if already initialized
            if firebase_admin._apps:
                return True

            firebase_credentials_path = os.getenv("FIREBASE_CREDENTIALS_PATH")
            cred = None
            project_id = None

            # Case 1: Use Streamlit secrets (Streamlit Cloud)
            if "firebase" in st.secrets:
                firebase_config = dict(st.secrets["firebase"])
                cred = credentials.Certificate(firebase_config)
                project_id = firebase_config.get("project_id")

            # Case 2: Use local firebase-service-account.json if available
            elif not firebase_credentials_path:
                default_path = os.path.join(os.path.dirname(__file__), "firebase-service-account.json")
                if os.path.exists(default_path):
                    firebase_credentials_path = default_path

            if firebase_credentials_path and os.path.exists(firebase_credentials_path):
                cred = credentials.Certificate(firebase_credentials_path)
                # Read project ID from file
                with open(firebase_credentials_path, 'r') as f:
                    cred_data = json.load(f)
                    project_id = cred_data.get("project_id")
            elif not cred:
                cred = credentials.ApplicationDefault()

            # Set project ID as environment variable if not already set
            if project_id and not os.getenv("GOOGLE_CLOUD_PROJECT"):
                os.environ["GOOGLE_CLOUD_PROJECT"] = project_id

            # Initialize with explicit options
            options = {}
            if project_id:
                options["projectId"] = project_id

            firebase_admin.initialize_app(cred, options)
            logger.info(f"Firebase Admin SDK initialized successfully for project: {project_id}")
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
        """Load up to 30 Reddit accounts from environment variables or Streamlit secrets."""
        accounts = {}
    
    # Try to load up to 30 accounts
        for i in range(1, 31):
            prefix = f"REDDIT_ACCOUNT_{i}"
            config = {
            "client_id": None,
            "client_secret": None,
            "username": None,
            "password": None,
            "user_agent": None,
        }
        
        # First try environment variables
            config["client_id"] = os.getenv(f"{prefix}_CLIENT_ID")
            config["client_secret"] = os.getenv(f"{prefix}_CLIENT_SECRET")
            config["username"] = os.getenv(f"{prefix}_USERNAME")
            config["password"] = os.getenv(f"{prefix}_PASSWORD")
            config["user_agent"] = os.getenv(f"{prefix}_USER_AGENT")
        
        # If not found in env vars, try Streamlit secrets
            if not all(config.values()):
                try:
                    import streamlit as st
                    if hasattr(st, 'secrets'):
                    # Try to get from Streamlit secrets
                        secrets = st.secrets
                    
                    # Check if there's a reddit section in secrets
                    if "reddit" in secrets:
                        reddit_secrets = secrets["reddit"]
                        
                        # Try account-specific keys first
                        account_key = f"account_{i}"
                        if account_key in reddit_secrets:
                            account_secrets = reddit_secrets[account_key]
                            config["client_id"] = account_secrets.get("client_id")
                            config["client_secret"] = account_secrets.get("client_secret")
                            config["username"] = account_secrets.get("username")
                            config["password"] = account_secrets.get("password")
                            config["user_agent"] = account_secrets.get("user_agent")
                        else:
                            # Try individual keys with account number suffix
                            config["client_id"] = config["client_id"] or reddit_secrets.get(f"client_id_{i}")
                            config["client_secret"] = config["client_secret"] or reddit_secrets.get(f"client_secret_{i}")
                            config["username"] = config["username"] or reddit_secrets.get(f"username_{i}")
                            config["password"] = config["password"] or reddit_secrets.get(f"password_{i}")
                            config["user_agent"] = config["user_agent"] or reddit_secrets.get(f"user_agent_{i}")
                    
                    # Also try top-level secrets with the full key name
                    for key, value in config.items():
                        if not value:
                            secret_key = f"{prefix}_{key.upper()}"
                            if secret_key in secrets:
                                config[key] = secrets[secret_key]
                                
                except Exception as e:
                    logger.warning(f"Could not access Streamlit secrets for {prefix}: {e}")
        
        # Check if all required fields are present
            if all(config.values()):
                account_name = config["username"]
                accounts[account_name] = config
                logger.info(f"Found account config: {account_name}")
            elif any(config.values()):
                logger.warning(f"Incomplete configuration for {prefix} - skipping")
    
    # Fallback to single account format (for backwards compatibility)
        if not accounts:
            config = {
            "client_id": os.getenv("REDDIT_CLIENT_ID"),
            "client_secret": os.getenv("REDDIT_CLIENT_SECRET"),
            "username": os.getenv("REDDIT_USERNAME"),
            "password": os.getenv("REDDIT_PASSWORD"),
            "user_agent": os.getenv("REDDIT_USER_AGENT"),
        }
        
        # Try Streamlit secrets for single account
            if not all(config.values()):
                try:
                    import streamlit as st
                    if hasattr(st, 'secrets'):
                        secrets = st.secrets
                    
                    # Try reddit section first
                        if "reddit" in secrets:
                            reddit_secrets = secrets["reddit"]
                            config["client_id"] = config["client_id"] or reddit_secrets.get("client_id")
                            config["client_secret"] = config["client_secret"] or reddit_secrets.get("client_secret")
                            config["username"] = config["username"] or reddit_secrets.get("username")
                            config["password"] = config["password"] or reddit_secrets.get("password")
                            config["user_agent"] = config["user_agent"] or reddit_secrets.get("user_agent")
                    
                        # Try top-level secrets
                        config["client_id"] = config["client_id"] or secrets.get("REDDIT_CLIENT_ID")
                        config["client_secret"] = config["client_secret"] or secrets.get("REDDIT_CLIENT_SECRET")
                        config["username"] = config["username"] or secrets.get("REDDIT_USERNAME")
                        config["password"] = config["password"] or secrets.get("REDDIT_PASSWORD")
                        config["user_agent"] = config["user_agent"] or secrets.get("REDDIT_USER_AGENT")
                    
                except Exception as e:
                    logger.warning(f"Could not access Streamlit secrets for single account: {e}")
        
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

    def get_user_posts(self, account_id: int, limit: int = 25, time_filter: str = "week") -> Dict[str, Any]:
        """Get posts from a specific account."""
        reddit_client = self.get_reddit_client(account_id)
        if not reddit_client:
            return {
                "success": False,
                "error": f"Invalid account_id: {account_id}. Use 1-{len(self.account_usernames)}"
            }
        
        username = self.get_account_username(account_id)
        
        try:
            user = reddit_client.user.me()
            posts = []
            
            # Get submissions
            submissions = user.submissions.new(limit=limit)
            
            for submission in submissions:
                # Filter by time if specified
                if time_filter != "all":
                    post_age = datetime.utcnow() - datetime.fromtimestamp(submission.created_utc)
                    if time_filter == "day" and post_age > timedelta(days=1):
                        continue
                    elif time_filter == "week" and post_age > timedelta(weeks=1):
                        continue
                    elif time_filter == "month" and post_age > timedelta(days=30):
                        continue
                
                post_data = {
                    "id": submission.id,
                    "title": submission.title,
                    "subreddit": submission.subreddit.display_name,
                    "score": submission.score,
                    "upvote_ratio": submission.upvote_ratio,
                    "num_comments": submission.num_comments,
                    "created_utc": submission.created_utc,
                    "created_time": datetime.fromtimestamp(submission.created_utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "url": f"https://reddit.com{submission.permalink}",
                    "is_self": submission.is_self,
                    "selftext": submission.selftext[:200] + "..." if len(submission.selftext) > 200 else submission.selftext,
                    "link_url": submission.url if not submission.is_self else None,
                    "nsfw": submission.over_18,
                    "spoiler": submission.spoiler,
                    "pinned": submission.stickied,
                    "archived": submission.archived,
                    "locked": submission.locked
                }
                posts.append(post_data)
            
            return {
                "success": True,
                "username": username,
                "posts": posts,
                "total_posts": len(posts),
                "time_filter": time_filter
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to fetch posts: {str(e)}"
            }

    def get_post_comments(self, post_id: str, account_id: int, limit: int = 50) -> Dict[str, Any]:
        """Get comments for a specific post."""
        reddit_client = self.get_reddit_client(account_id)
        if not reddit_client:
            return {
                "success": False,
                "error": f"Invalid account_id: {account_id}. Use 1-{len(self.account_usernames)}"
            }
        
        try:
            submission = reddit_client.submission(id=post_id)
            submission.comments.replace_more(limit=0)  # Flatten comment tree
            
            comments = []
            for comment in submission.comments.list()[:limit]:
                if hasattr(comment, 'body'):  # Make sure it's a comment, not MoreComments
                    comment_data = {
                        "id": comment.id,
                        "author": str(comment.author) if comment.author else "[deleted]",
                        "body": comment.body,
                        "score": comment.score,
                        "created_utc": comment.created_utc,
                        "created_time": datetime.fromtimestamp(comment.created_utc).strftime("%Y-%m-%d %H:%M:%S"),
                        "is_submitter": comment.is_submitter,
                        "parent_id": comment.parent_id,
                        "permalink": f"https://reddit.com{comment.permalink}",
                        "depth": comment.depth
                    }
                    comments.append(comment_data)
            
            return {
                "success": True,
                "post_id": post_id,
                "post_title": submission.title,
                "comments": comments,
                "total_comments": len(comments)
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to fetch comments: {str(e)}"
            }

    def reply_to_comment(self, comment_id: str, reply_text: str, account_id: int) -> Dict[str, Any]:
        """Reply to a specific comment."""
        if not reply_text.strip():
            return {
                "success": False,
                "error": "Reply text cannot be empty"
            }
        
        reddit_client = self.get_reddit_client(account_id)
        if not reddit_client:
            return {
                "success": False,
                "error": f"Invalid account_id: {account_id}. Use 1-{len(self.account_usernames)}"
            }
        
        username = self.get_account_username(account_id)
        
        try:
            comment = reddit_client.comment(id=comment_id)
            reply = comment.reply(reply_text)
            
            return {
                "success": True,
                "message": "Reply posted successfully",
                "reply_details": {
                    "reply_id": reply.id,
                    "reply_url": f"https://reddit.com{reply.permalink}",
                    "parent_comment_id": comment_id,
                    "account_used": username,
                    "reply_text": reply_text[:100] + "..." if len(reply_text) > 100 else reply_text
                }
            }
            
        except prawcore.exceptions.Forbidden as e:
            return {
                "success": False,
                "error": f"Forbidden: Account '{username}' cannot reply to this comment"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to post reply: {str(e)}"
            }
    def post_scheduled_comment(self, post_url: str, comment_text: str, account_id: int) -> Dict[str, Any]:
        """Post a comment to a Reddit post using URL."""
        if not comment_text.strip():
            return {"success": False, "error": "Comment text cannot be empty"}
    
        reddit_client = self.get_reddit_client(account_id)
        if not reddit_client:
            return {
            "success": False,
            "error": f"Invalid account_id: {account_id}. Use 1-{len(self.account_usernames)}"
        }
    
        username = self.get_account_username(account_id)
    
        try:
            # Extract post ID from URL
            if "/comments/" in post_url:
                post_id = post_url.split("/comments/")[1].split("/")[0]
            else:
                return {"success": False, "error": "Invalid Reddit post URL"}
        
            submission = reddit_client.submission(id=post_id)
            comment = submission.reply(comment_text)
        
            return {
            "success": True,
            "message": "Comment posted successfully",
            "comment_details": {
                "comment_id": comment.id,
                "comment_url": f"https://reddit.com{comment.permalink}",
                "post_id": post_id,
                "account_used": username,
                "comment_text": comment_text[:100] + "..." if len(comment_text) > 100 else comment_text
            }
        }
        
        except Exception as e:
            return {"success": False, "error": f"Failed to post comment: {str(e)}"}
    
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
        
class CommentScheduler:
    """Handle scheduled comment posting."""
    
    def __init__(self, reddit_core):
        self.reddit_core = reddit_core
        self.scheduled_jobs = []
        self.scheduler_thread = None
        self.running = False
    
    def schedule_comment(self, post_url: str, comment_text: str, account_id: int, scheduled_time: datetime) -> Dict[str, Any]:
        """Schedule a comment to be posted at a specific time."""
        try:
            job_id = f"comment_{len(self.scheduled_jobs)}_{int(scheduled_time.timestamp())}"
            
            def post_job():
                result = self.reddit_core.post_scheduled_comment(post_url, comment_text, account_id)
                logger.info(f"Scheduled comment job {job_id} completed: {result}")
                # Remove completed job from list
                self.scheduled_jobs = [job for job in self.scheduled_jobs if job['id'] != job_id]
            
            # Schedule the job
            schedule.every().day.at(scheduled_time.strftime("%H:%M")).do(post_job).tag(job_id)
            
            # Add to tracking list
            job_info = {
                'id': job_id,
                'post_url': post_url,
                'comment_text': comment_text[:50] + "..." if len(comment_text) > 50 else comment_text,
                'account_id': account_id,
                'scheduled_time': scheduled_time,
                'status': 'pending'
            }
            self.scheduled_jobs.append(job_info)
            
            # Start scheduler if not running
            if not self.running:
                self.start_scheduler()
            
            return {
                "success": True,
                "message": f"Comment scheduled for {scheduled_time.strftime('%Y-%m-%d %H:%M')}",
                "job_id": job_id
            }
            
        except Exception as e:
            return {"success": False, "error": f"Failed to schedule comment: {str(e)}"}
    
    def start_scheduler(self):
        """Start the scheduler thread."""
        if not self.running:
            self.running = True
            self.scheduler_thread = threading.Thread(target=self._run_scheduler, daemon=True)
            self.scheduler_thread.start()
    
    def _run_scheduler(self):
        """Run the scheduler in a separate thread."""
        while self.running:
            schedule.run_pending()
            time.sleep(60)  # Check every minute
    
    def get_scheduled_jobs(self):
        """Get list of scheduled jobs."""
        return self.scheduled_jobs
    
    def cancel_job(self, job_id: str) -> bool:
        """Cancel a scheduled job."""
        try:
            schedule.clear(job_id)
            self.scheduled_jobs = [job for job in self.scheduled_jobs if job['id'] != job_id]
            return True
        except:
            return False

# Initialize components
@st.cache_resource
def init_components():
    """Initialize Firebase and Reddit components."""
    firebase_auth = FirebaseAuth()
    reddit_core = RedditCore()
    comment_scheduler = CommentScheduler(reddit_core)
    return firebase_auth, reddit_core, comment_scheduler

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

def render_main_app(firebase_auth, reddit_core, comment_scheduler):
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
        st.warning("⚠️ Please load Reddit accounts first using the sidebar button.")
        return
    
    accounts = st.session_state.get('reddit_accounts', [])
    
    # Create tabs for different functionalities
    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(["Create Post", "Verify Subreddit", "My Posts", "Comments", "Get Flairs", "Schedule Comments"])
    
    with tab1:
        st.header("Create New Post")
        
        # Account selection
        if not accounts:
            st.error("No accounts available")
            return
            
        account_options = [f"{acc['id']}. {acc['username']}" for acc in accounts]
        selected_account = st.selectbox("Select Account", account_options)
        account_id = int(selected_account.split('.')[0])
        
        # Post form
        with st.form("post_form"):
            col1, col2 = st.columns([2, 1])
            
            with col1:
                title = st.text_input("Post Title*", placeholder="Enter your post title...")
                subreddit_name = st.text_input("Subreddit*", placeholder="python, askreddit, etc. (without r/)")
            
            with col2:
                post_type = st.selectbox("Post Type", ["Text Post", "Link Post", "Image Post"])
                nsfw = st.checkbox("NSFW")
                spoiler = st.checkbox("Spoiler")
                send_replies = st.checkbox("Send Reply Notifications", value=True)
            
            # Content based on post type
            if post_type == "Text Post":
                body = st.text_area("Post Content", placeholder="Write your post content here...", height=200)
                url = None
                image_path = None
            elif post_type == "Link Post":
                url = st.text_input("URL*", placeholder="https://example.com")
                body = None
                image_path = None
            else:  # Image Post
                uploaded_file = st.file_uploader("Choose an image", type=['png', 'jpg', 'jpeg', 'gif'])
                if uploaded_file:
                    # Save uploaded file temporarily
                    import tempfile
                    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{uploaded_file.name.split('.')[-1]}") as tmp_file:
                        tmp_file.write(uploaded_file.getvalue())
                        image_path = tmp_file.name
                else:
                    image_path = None
                body = None
                url = None
            
            # Flair options
            st.markdown("### Flair (Optional)")
            col1, col2 = st.columns(2)
            with col1:
                flair_id = st.text_input("Flair ID", placeholder="Optional flair template ID")
            with col2:
                flair_text = st.text_input("Flair Text", placeholder="Optional custom flair text")
            
            submitted = st.form_submit_button("🚀 Post to Reddit", type="primary")
            
            if submitted:
                if not title or not subreddit_name:
                    st.error("Title and Subreddit are required!")
                elif post_type == "Link Post" and not url:
                    st.error("URL is required for link posts!")
                elif post_type == "Image Post" and not image_path:
                    st.error("Please upload an image!")
                else:
                    # Prepare post data
                    post_data = {
                        "account_id": account_id,
                        "subreddit_name": subreddit_name.strip(),
                        "title": title.strip(),
                        "body": body,
                        "url": url,
                        "image_path": image_path,
                        "flair_id": flair_id.strip() if flair_id else None,
                        "flair_text": flair_text.strip() if flair_text else None,
                        "nsfw": nsfw,
                        "spoiler": spoiler,
                        "send_replies": send_replies
                    }
                    
                    with st.spinner("Posting to Reddit..."):
                        result = reddit_core.post_content(post_data)
                    
                    if result["success"]:
                        st.success(f"✅ {result['message']}")
                        post_details = result["post_details"]
                        
                        # Display post details with prominent Post ID
                        st.markdown("### Post Details")
                        
                        # Highlight Post ID prominently
                        st.markdown("#### 🆔 Post ID (copy this to view comments)")
                        st.code(post_details['post_id'], language=None)
                        
                        col1, col2 = st.columns(2)
                        
                        with col1:
                            st.write(f"**Post Type:** {post_details['post_type']}")
                            st.write(f"**Account Used:** {post_details['account_used']}")
                            st.write(f"**Subreddit:** r/{post_details['subreddit']}")
                        
                        with col2:
                            st.write(f"**NSFW:** {post_details['nsfw']}")
                            st.write(f"**Spoiler:** {post_details['spoiler']}")
                            st.write(f"**Flair Applied:** {post_details['flair_applied']}")
                        
                        if post_details['post_url']:
                            st.markdown(f"🔗 **[View Post on Reddit]({post_details['post_url']})**")
                        
                        st.info("💡 Copy the Post ID above to use in the Comments tab to view and reply to comments!")
                    else:
                        st.error(f"❌ Failed to post: {result['error']}")
                        if 'details' in result:
                            st.error(f"Details: {result['details']}")
    
    with tab2:
        st.header("Verify Subreddit")
        
        col1, col2 = st.columns([2, 1])
        
        with col1:
            verify_subreddit = st.text_input("Subreddit to verify", placeholder="python, askreddit, etc.")
        
        with col2:
            verify_account = st.selectbox("Using Account", account_options, key="verify_account")
            verify_account_id = int(verify_account.split('.')[0])
        
        if st.button("🔍 Verify Subreddit"):
            if verify_subreddit:
                with st.spinner("Verifying subreddit..."):
                    result = reddit_core.verify_subreddit(verify_subreddit.strip(), verify_account_id)
                
                if result["success"]:
                    subreddit_info = result["subreddit"]
                    st.success(f"✅ r/{subreddit_info['display_name']} is accessible!")
                    
                    # Display subreddit info
                    col1, col2 = st.columns(2)
                    
                    with col1:
                        st.metric("Subscribers", f"{subreddit_info['subscribers']:,}")
                        st.write(f"**NSFW:** {'Yes' if subreddit_info['nsfw'] else 'No'}")
                    
                    with col2:
                        st.write("**Description:**")
                        st.write(subreddit_info['description'][:200] + "..." if len(subreddit_info['description']) > 200 else subreddit_info['description'])
                else:
                    st.error(f"❌ {result['error']}")
            else:
                st.warning("Please enter a subreddit name")
    
    with tab3:
        st.header("My Posts")
        
        col1, col2, col3 = st.columns([2, 1, 1])
        
        with col1:
            posts_account = st.selectbox("Select Account", account_options, key="posts_account")
            posts_account_id = int(posts_account.split('.')[0])
        
        with col2:
            time_filter = st.selectbox("Time Filter", ["day", "week", "month", "all"])
        
        with col3:
            post_limit = st.number_input("Limit", min_value=1, max_value=100, value=25)
        
        if st.button("📊 Get My Posts"):
            with st.spinner("Fetching posts..."):
                result = reddit_core.get_user_posts(posts_account_id, post_limit, time_filter)
            
            if result["success"]:
                posts = result["posts"]
                st.success(f"✅ Found {len(posts)} posts for u/{result['username']}")
                
                if posts:
                    for post in posts:
                        # Create expandable section with Post ID prominently displayed
                        with st.expander(f"📝 {post['title'][:70]}... | r/{post['subreddit']} | {post['score']} pts | ID: {post['id']}"):
                            
                            # Prominently display Post ID at the top
                            st.markdown("#### 🆔 Post ID (copy to view comments)")
                            col_id, col_copy = st.columns([3, 1])
                            with col_id:
                                st.code(post['id'], language=None)
                            with col_copy:
                                st.button(f"📋 Copy", key=f"copy_{post['id']}", help="Click to select Post ID")
                            
                            st.markdown("---")
                            
                            col1, col2 = st.columns([2, 1])
                            
                            with col1:
                                st.write(f"**Title:** {post['title']}")
                                st.write(f"**Subreddit:** r/{post['subreddit']}")
                                if post['is_self'] and post['selftext']:
                                    st.write(f"**Content:** {post['selftext']}")
                                elif not post['is_self'] and post['link_url']:
                                    st.write(f"**Link:** {post['link_url']}")
                            
                            with col2:
                                st.metric("Score", post['score'])
                                st.metric("Comments", post['num_comments'])
                                st.write(f"**Created:** {post['created_time']}")
                                st.write(f"**Upvote Ratio:** {post['upvote_ratio']:.1%}")
                                
                                if post['nsfw']:
                                    st.warning("NSFW")
                                if post['spoiler']:
                                    st.warning("Spoiler")
                                if post['locked']:
                                    st.warning("Locked")
                                
                                st.markdown(f"[View on Reddit]({post['url']})")
                            
                            # Add quick action to navigate to comments
                            if post['num_comments'] > 0:
                                st.info(f"💬 This post has {post['num_comments']} comments. Copy the Post ID above and use it in the Comments tab!")
                else:
                    st.info("No posts found for the selected time period.")
            else:
                st.error(f"❌ {result['error']}")
    
    with tab4:
        st.header("Post Comments")
        
        # Add helpful instruction at the top
        st.info("💡 Copy a Post ID from the 'My Posts' tab or from a successful post creation to view its comments here.")
        
        col1, col2 = st.columns([2, 1])
        
        with col1:
            post_id = st.text_input("Post ID", placeholder="Enter Reddit post ID (e.g., abc123)")
            comment_account = st.selectbox("Using Account", account_options, key="comment_account")
            comment_account_id = int(comment_account.split('.')[0])
        
        with col2:
            comment_limit = st.number_input("Comment Limit", min_value=1, max_value=200, value=50)
        
        if st.button("💬 Get Comments") and post_id:
            with st.spinner("Fetching comments..."):
                result = reddit_core.get_post_comments(post_id.strip(), comment_account_id, comment_limit)
            
            if result["success"]:
                comments = result["comments"]
                st.success(f"✅ Found {len(comments)} comments for: {result['post_title']}")
                
                # Display Post ID being viewed
                st.markdown("#### 📝 Currently viewing comments for Post ID:")
                st.code(result['post_id'], language=None)
                
                if comments:
                    # Reply form
                    with st.form("reply_form"):
                        st.markdown("### Reply to a Comment")
                        
                        # Select comment to reply to
                        comment_options = [f"{c['id']} - {c['author']}: {c['body'][:50]}..." for c in comments[:20]]  # Limit options for UI
                        selected_comment = st.selectbox("Select Comment to Reply To", comment_options)
                        reply_text = st.text_area("Your Reply", placeholder="Type your reply here...")
                        
                        if st.form_submit_button("Reply"):
                            if reply_text.strip():
                                comment_id = selected_comment.split(' - ')[0]
                                with st.spinner("Posting reply..."):
                                    reply_result = reddit_core.reply_to_comment(comment_id, reply_text, comment_account_id)
                                
                                if reply_result["success"]:
                                    st.success(f"✅ Reply posted successfully!")
                                    reply_details = reply_result["reply_details"]
                                    st.markdown(f"[View Reply]({reply_details['reply_url']})")
                                else:
                                    st.error(f"❌ {reply_result['error']}")
                            else:
                                st.error("Reply text cannot be empty")
                    
                    st.markdown("---")
                    st.markdown("### Comments")
                    
                    for comment in comments:
                        indent = "  " * comment['depth']  # Indent based on comment depth
                        with st.expander(f"{indent}💬 u/{comment['author']} | {comment['score']} pts | {comment['created_time']}"):
                            st.write(f"**Comment ID:** {comment['id']}")
                            st.write(f"**Author:** u/{comment['author']}")
                            st.write(f"**Score:** {comment['score']}")
                            st.write(f"**Depth:** {comment['depth']}")
                            st.write(f"**Created:** {comment['created_time']}")
                            if comment['is_submitter']:
                                st.info("👑 Original Poster")
                            
                            st.markdown("**Comment:**")
                            st.write(comment['body'])
                            
                            st.markdown(f"[View on Reddit]({comment['permalink']})")
                else:
                    st.info("No comments found for this post.")
            else:
                st.error(f"❌ {result['error']}")
    
    with tab5:
        st.header("Get Subreddit Flairs")
        
        col1, col2 = st.columns([2, 1])
        
        with col1:
            flair_subreddit = st.text_input("Subreddit", placeholder="python, askreddit, etc.")
        
        with col2:
            flair_account = st.selectbox("Using Account", account_options, key="flair_account")
            flair_account_id = int(flair_account.split('.')[0])
        
        if st.button("🏷️ Get Flairs") and flair_subreddit:
            with st.spinner("Fetching flairs..."):
                result = reddit_core.get_flairs(flair_subreddit.strip(), flair_account_id)
            
            if result["success"]:
                flairs = result["flairs"]
                st.success(f"✅ Found {result['flair_count']} flairs for r/{result['subreddit']}")
                
                if flairs:
                    st.markdown("### Available Flairs")
                    
                    for i, flair in enumerate(flairs, 1):
                        with st.expander(f"🏷️ Flair {i}: {flair['text'] or 'No text'}"):
                            col1, col2 = st.columns(2)
                            
                            with col1:
                                st.write(f"**ID:** `{flair['id']}`")
                                st.write(f"**Text:** {flair['text'] or 'None'}")
                                st.write(f"**Editable:** {'Yes' if flair['text_editable'] else 'No'}")
                            
                            with col2:
                                st.write(f"**Text Color:** {flair['text_color'] or 'Default'}")
                                st.write(f"**Background Color:** {flair['background_color'] or 'Default'}")
                                
                                # Copy button for flair ID
                                st.code(flair['id'], language=None)
                                st.caption("↑ Copy this ID to use in posts")
                else:
                    st.info("No flairs found for this subreddit.")
            else:
                st.error(f"❌ {result['error']}")

    with tab6:
        st.header("Schedule Comments")
    
    # Schedule new comment section
        st.subheader("Schedule New Comment")
    
        with st.form("schedule_comment_form"):
            col1, col2 = st.columns([2, 1])
        
        with col1:
            post_url = st.text_input("Reddit Post URL*", placeholder="https://www.reddit.com/r/subreddit/comments/...")
            comment_text = st.text_area("Comment Text*", placeholder="Your comment here...", height=100)
        
        with col2:
            comment_account = st.selectbox("Select Account", account_options, key="schedule_account")
            comment_account_id = int(comment_account.split('.')[0])
            
            # Date and time selection
            schedule_date = st.date_input("Schedule Date", min_value=datetime.now().date())
            schedule_time = st.time_input("Schedule Time")
        
            submitted = st.form_submit_button("⏰ Schedule Comment", type="primary")
        
            if submitted:
                if not post_url or not comment_text:
                    st.error("Post URL and Comment Text are required!")
                else:
                    # Combine date and time
                    scheduled_datetime = datetime.combine(schedule_date, schedule_time)
                    
                    # Check if scheduled time is in the future
                    if scheduled_datetime <= datetime.now():
                        st.error("Scheduled time must be in the future!")
                    else:
                        result = comment_scheduler.schedule_comment(
                            post_url.strip(), 
                            comment_text.strip(), 
                            comment_account_id, 
                            scheduled_datetime
                        )
                        
                        if result["success"]:
                            st.success(f"✅ {result['message']}")
                            st.info(f"Job ID: {result['job_id']}")
                        else:
                            st.error(f"❌ {result['error']}")
        
        st.markdown("---")
        
        # Show scheduled jobs
        st.subheader("Scheduled Comments")
        
        scheduled_jobs = comment_scheduler.get_scheduled_jobs()
        
        if scheduled_jobs:
            for job in scheduled_jobs:
                with st.expander(f"⏰ {job['scheduled_time'].strftime('%Y-%m-%d %H:%M')} | {job['comment_text']}"):
                    col1, col2 = st.columns([3, 1])
                    
                    with col1:
                        st.write(f"**Job ID:** {job['id']}")
                        st.write(f"**Post URL:** {job['post_url']}")
                        st.write(f"**Comment:** {job['comment_text']}")
                        st.write(f"**Account:** {accounts[job['account_id']-1]['username']}")
                        st.write(f"**Scheduled:** {job['scheduled_time'].strftime('%Y-%m-%d %H:%M:%S')}")
                        st.write(f"**Status:** {job['status']}")
                    
                    with col2:
                        if st.button("🗑️ Cancel", key=f"cancel_{job['id']}"):
                            if comment_scheduler.cancel_job(job['id']):
                                st.success("Job cancelled!")
                                st.rerun()
                            else:
                                st.error("Failed to cancel job")
        else:
            st.info("No scheduled comments")

def main():
    """Main application entry point."""
    # Initialize components
    firebase_auth, reddit_core, comment_scheduler = init_components()
    
    # Check authentication
    if not st.session_state.get("authenticated", False):
        render_login_page(firebase_auth)
    else:
        render_main_app(firebase_auth, reddit_core, comment_scheduler)

if __name__ == "__main__":
    main()