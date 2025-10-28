"""
A simple python script to export all watched videos (movies, anime, shows) history from SIMKL to csv files.

Simkl API Setup:
1. Go to "https://simkl.com/settings/developer/" and create a new app.
2. Use "urn:ietf:wg:oauth:2.0:oob" for the "Redirect URI" section.

How to use the script:
1. Add your client_id below (replace the placeholder ID).
2. Run the script.
3. Trim the generated files as you need, then import the data into other platforms.
"""
import requests
import pandas as pd


def make_request(url, headers = None):
    response = requests.get(url, headers=headers)
    return response.json()


# --- Configuration and Initialization ---
# **IMPORTANT: Replace this with your actual Simkl client_id**
client_id = ""


# --- Step 1: Get the User Code for OAuth Authentication (PIN flow) ---
get_pin_url = f"https://api.simkl.com/oauth/pin?client_id={client_id}"
pin_request = make_request(get_pin_url)

user_code = pin_request["user_code"]
verification_url = pin_request["verification_url"]

is_user_authenticated = False
code_verification_url = f"https://api.simkl.com/oauth/pin/{user_code}?client_id={client_id}"


# --- Step 2: User Authentication Loop (Wait for User to Authorize) ---
while not is_user_authenticated:
    print(f"GO to {verification_url} and input the following code")
    print(user_code)
    input("Press \"ANY\" after confirmation...")
    code_verification_request = make_request(code_verification_url)
    if "access_token" in code_verification_request:
        access_token = code_verification_request["access_token"]
        is_user_authenticated = True


# --- Step 3: Fetch and Export Watched Videos ---
video_types = ["movies", "anime", "shows"]
get_videos_list_url = f"https://api.simkl.com/sync/all-items/"
raw_data = make_request(get_videos_list_url,
                            {'Authorization': f"Bearer {access_token}", "simkl-api-key": client_id})

for video_type in video_types:
    data_tag = "movie" if video_type == "movies" else "show"

    # Initial and normalize a dataframe
    df = pd.DataFrame(raw_data[video_type])
    video_data = pd.json_normalize(df[data_tag], max_level=0)

    # Obtain targeted data
    video_ids = pd.json_normalize(video_data["ids"]).add_suffix("_id")
    video_names = video_data["title"]
    watched_time = df["last_watched_at"]

    # Finalize and save data as csv file
    df = pd.concat((video_ids, video_names, watched_time), axis=1)
    df.to_csv(f"{video_type}_data.csv", index=False)
