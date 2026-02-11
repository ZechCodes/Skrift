from skrift.auth.roles import register_role, ROLE_DEFINITIONS

# Add moderate-tweets permission to the existing moderator role
ROLE_DEFINITIONS["moderator"].permissions.add("moderate-tweets")

# Register a dedicated tweet-moderator role
register_role(
    "tweet-moderator",
    "moderate-tweets",
    display_name="Tweet Moderator",
    description="Can moderate tweets in the admin panel",
)
