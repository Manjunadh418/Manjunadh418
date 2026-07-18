import datetime
import hashlib
import os

import requests
from dateutil import relativedelta
from lxml import etree

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
# Fine-grained PAT stored as a repo secret called ACCESS_TOKEN, with:
#   Account permissions:    read:Followers, read:Starring, read:Watching
#   Repository permissions: read:Commit statuses, read:Contents,
#                            read:Issues, read:Metadata, read:Pull Requests
HEADERS = {"authorization": "token " + os.environ["ACCESS_TOKEN"]}
USER_NAME = os.environ.get("USER_NAME", "Manjunadh418")

# Set this to your own birthday/join-date reference if you want an "age" stat.
# Using GitHub account creation date by default (fetched at runtime).

QUERY_COUNT = {
    "user_getter": 0,
    "follower_getter": 0,
    "graph_repos_stars": 0,
    "recursive_loc": 0,
    "graph_commits": 0,
    "loc_query": 0,
}


def query_count(funct_id):
    QUERY_COUNT[funct_id] += 1


def simple_request(func_name, query, variables):
    request = requests.post(
        "https://api.github.com/graphql",
        json={"query": query, "variables": variables},
        headers=HEADERS,
    )
    if request.status_code == 200:
        return request
    raise Exception(func_name, "failed with", request.status_code, request.text, QUERY_COUNT)


# --------------------------------------------------------------------------
# Account age
# --------------------------------------------------------------------------
def format_plural(unit):
    return "s" if unit != 1 else ""


def account_age(created_at):
    created = datetime.datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
    diff = relativedelta.relativedelta(datetime.datetime.today(), created)
    return "{} {}, {} {}, {} {}".format(
        diff.years, "year" + format_plural(diff.years),
        diff.months, "month" + format_plural(diff.months),
        diff.days, "day" + format_plural(diff.days),
    )


def user_getter(username):
    query_count("user_getter")
    query = """
    query($login: String!){
      user(login: $login) {
        id
        createdAt
      }
    }"""
    request = simple_request(user_getter.__name__, query, {"login": username})
    data = request.json()["data"]["user"]
    return {"id": data["id"]}, data["createdAt"]


def follower_getter(username):
    query_count("follower_getter")
    query = """
    query($login: String!){
      user(login: $login) {
        followers { totalCount }
      }
    }"""
    request = simple_request(follower_getter.__name__, query, {"login": username})
    return int(request.json()["data"]["user"]["followers"]["totalCount"])


# --------------------------------------------------------------------------
# Repos / stars / commits
# --------------------------------------------------------------------------
def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    query_count("graph_repos_stars")
    query = """
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
      user(login: $login) {
        repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
          totalCount
          edges {
            node {
              ... on Repository {
                nameWithOwner
                stargazers { totalCount }
              }
            }
          }
          pageInfo { endCursor hasNextPage }
        }
      }
    }"""
    variables = {"owner_affiliation": owner_affiliation, "login": USER_NAME, "cursor": cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    repos = request.json()["data"]["user"]["repositories"]
    if count_type == "repos":
        return repos["totalCount"]
    elif count_type == "stars":
        return sum(edge["node"]["stargazers"]["totalCount"] for edge in repos["edges"])


def graph_commits(start_date, end_date):
    query_count("graph_commits")
    query = """
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
      user(login: $login) {
        contributionsCollection(from: $start_date, to: $end_date) {
          contributionCalendar { totalContributions }
        }
      }
    }"""
    variables = {"start_date": start_date, "end_date": end_date, "login": USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(
        request.json()["data"]["user"]["contributionsCollection"]["contributionCalendar"][
            "totalContributions"
        ]
    )


# --------------------------------------------------------------------------
# Lines of code (walks every commit in every owned/collaborator/org repo)
# --------------------------------------------------------------------------
def loc_query(owner_affiliation, cursor=None, edges=None):
    query_count("loc_query")
    edges = edges or []
    query = """
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
      user(login: $login) {
        repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
          edges {
            node {
              ... on Repository {
                nameWithOwner
                defaultBranchRef {
                  target {
                    ... on Commit { history { totalCount } }
                  }
                }
              }
            }
          }
          pageInfo { endCursor hasNextPage }
        }
      }
    }"""
    variables = {"owner_affiliation": owner_affiliation, "login": USER_NAME, "cursor": cursor}
    request = simple_request(loc_query.__name__, query, variables)
    repos = request.json()["data"]["user"]["repositories"]
    edges += repos["edges"]
    if repos["pageInfo"]["hasNextPage"]:
        return loc_query(owner_affiliation, repos["pageInfo"]["endCursor"], edges)
    return cache_builder(edges)


def cache_builder(edges):
    """
    Uses a per-user cache file keyed by repo name-hash + last-seen commit count,
    so unchanged repos aren't re-walked (LOC counting is the expensive part).
    """
    filename = "cache/" + hashlib.sha256(USER_NAME.encode("utf-8")).hexdigest() + ".txt"
    try:
        with open(filename, "r") as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []

    if len(data) != len(edges):
        data = []
        with open(filename, "w") as f:
            for edge in edges:
                repo_hash = hashlib.sha256(edge["node"]["nameWithOwner"].encode("utf-8")).hexdigest()
                f.write(f"{repo_hash} 0 0 0 0\n")
        with open(filename, "r") as f:
            data = f.readlines()

    loc_add, loc_del = 0, 0
    for index, edge in enumerate(edges):
        repo_hash, commit_count, *_ = data[index].split()
        default_branch = edge["node"]["defaultBranchRef"]
        current_commit_count = default_branch["target"]["history"]["totalCount"] if default_branch else 0

        if int(commit_count) != current_commit_count:
            owner, repo_name = edge["node"]["nameWithOwner"].split("/")
            add, dele, commits = recursive_loc(owner, repo_name)
            data[index] = f"{repo_hash} {current_commit_count} {commits} {add} {dele}\n"

        with open(filename, "w") as f:
            f.writelines(data)

    for line in data:
        _, _, _, add, dele = line.split()
        loc_add += int(add)
        loc_del += int(dele)

    return [loc_add, loc_del, loc_add - loc_del]


def recursive_loc(owner, repo_name, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    query_count("recursive_loc")
    query = """
    query ($repo_name: String!, $owner: String!, $cursor: String) {
      repository(name: $repo_name, owner: $owner) {
        defaultBranchRef {
          target {
            ... on Commit {
              history(first: 100, after: $cursor) {
                edges {
                  node {
                    ... on Commit {
                      author { user { id } }
                      additions
                      deletions
                    }
                  }
                }
                pageInfo { endCursor hasNextPage }
              }
            }
          }
        }
      }
    }"""
    variables = {"repo_name": repo_name, "owner": owner, "cursor": cursor}
    request = simple_request(recursive_loc.__name__, query, variables)
    branch = request.json()["data"]["repository"]["defaultBranchRef"]
    if branch is None:
        return 0, 0, 0
    history = branch["target"]["history"]
    for edge in history["edges"]:
        author = edge["node"]["author"]["user"]
        if author is not None and author.get("id") == OWNER_ID.get("id"):
            my_commits += 1
            addition_total += edge["node"]["additions"]
            deletion_total += edge["node"]["deletions"]
    if history["pageInfo"]["hasNextPage"]:
        return recursive_loc(
            owner, repo_name, addition_total, deletion_total, my_commits, history["pageInfo"]["endCursor"]
        )
    return addition_total, deletion_total, my_commits


# --------------------------------------------------------------------------
# SVG writing
# --------------------------------------------------------------------------
def find_and_replace(root, element_id, new_text):
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def justify_format(root, element_id, new_text, length=0):
    if isinstance(new_text, int):
        new_text = "{:,}".format(new_text)
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    dot_map = {0: "", 1: " ", 2: ". "}
    dot_string = dot_map.get(just_len, " " + ("." * just_len) + " ")
    find_and_replace(root, f"{element_id}_dots", dot_string)


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, follower_data, loc_data):
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, "age_data", age_data)
    justify_format(root, "commit_data", commit_data, 22)
    justify_format(root, "star_data", star_data, 14)
    justify_format(root, "repo_data", repo_data, 6)
    justify_format(root, "follower_data", follower_data, 10)
    justify_format(root, "loc_data", loc_data[2], 9)
    justify_format(root, "loc_add", loc_data[0])
    justify_format(root, "loc_del", loc_data[1], 7)
    tree.write(filename, encoding="utf-8", xml_declaration=True)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Building stats card for {USER_NAME}...")

    user_data, created_at = user_getter(USER_NAME)
    OWNER_ID = user_data

    age_data = account_age(created_at)
    star_data = graph_repos_stars("stars", ["OWNER"])
    repo_data = graph_repos_stars("repos", ["OWNER"])
    follower_data = follower_getter(USER_NAME)

    now = datetime.datetime.utcnow()
    commit_data = graph_commits(
        (now - datetime.timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    loc_data = loc_query(["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"])

    svg_overwrite("dark_mode.svg", age_data, commit_data, star_data, repo_data, follower_data, loc_data)
    svg_overwrite("light_mode.svg", age_data, commit_data, star_data, repo_data, follower_data, loc_data)

    print("Done. GraphQL calls made:", sum(QUERY_COUNT.values()))
    for name, count in QUERY_COUNT.items():
        print(f"  {name}: {count}")
