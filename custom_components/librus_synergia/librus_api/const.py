"""Low-level constants for talking to Librus's Synergia/API gateway.

Librus changed its authentication mechanism around 2026-03-28 (confirmed via
the `emsi/librus_pyapi` project's changelog and by our own live testing on
2026-09-05): the older `api.librus.pl/OAuth/Token` password-grant flow that
szkolny-eu/szkolny-android (GPL-3.0) documented now returns
`unsupported_grant_type`. The flow implemented here - a login form POST to
the `/OAuth/Authorization` endpoint followed by a manually-walked redirect
chain - is reverse-engineered from `emsi/librus_pyapi` (MIT-licensed, updated
for this exact change) and confirmed live to complete WITHOUT a captcha
challenge for a normal login.

Unlike the old flow, this one is cookie/session-based, not bearer-token
based: there is no OAuth `access_token`/`refresh_token` pair, only a
same-day `oauth_token` session cookie (confirmed live to expire ~24h after
login) plus a long-lived (~1 year) `DeviceCookie` on api.librus.pl that is
almost certainly why a normal login skips captcha/2FA - it marks the client
as a previously-seen device. Persisting and re-sending that cookie jar
across logins (and across Home Assistant restarts) is important for staying
captcha-free long-term, not just a nice-to-have.
"""

from __future__ import annotations

CLIENT_ID = "46"

SYNERGIA_DOMAIN = "synergia.librus.pl"
API_DOMAIN = "api.librus.pl"

SYNERGIA_PORTAL_LOGIN_URL = "https://synergia.librus.pl/loguj/portalRodzina"
API_OAUTH_AUTHORIZATION_URL = f"https://api.librus.pl/OAuth/Authorization?client_id={CLIENT_ID}"
API_OAUTH_AUTHORIZATION_WITH_SCOPE_URL = (
    f"{API_OAUTH_AUTHORIZATION_URL}&response_type=code&scope=mydata"
)
MAX_OAUTH_REDIRECTS = 10

OAUTH_TOKEN_COOKIE = "oauth_token"
DEVICE_COOKIE = "DeviceCookie"

# Data endpoints live behind the Synergia gateway now, authenticated by the
# session cookies obtained above - confirmed live against `Me`.
DATA_BASE_URL = "https://synergia.librus.pl/gateway/api/2.0"

# A real desktop browser UA making an XHR request - this is what the login
# endpoint (an AJAX-style form POST, not a bare mobile-app API call) expects;
# using the old mobile-app UA here is untested and the confirmed-working
# combination is kept as-is rather than guessed at.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

LOGIN_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9,pl;q=0.8",
    "Cache-Control": "no-cache",
    "Origin": f"https://{API_DOMAIN}",
    "Pragma": "no-cache",
    "Referer": API_OAUTH_AUTHORIZATION_URL,
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": USER_AGENT,
    "X-Requested-With": "XMLHttpRequest",
}

# Cookies worth persisting across Home Assistant restarts, keyed by the
# domain they belong to. DeviceCookie in particular is the ~1 year, likely
# captcha/2FA-avoiding one - see the module docstring.
PERSISTED_COOKIE_NAMES: dict[str, tuple[str, ...]] = {
    SYNERGIA_DOMAIN: ("oauth_token", "DZIENNIKSID", "SDZIENNIKSID"),
    API_DOMAIN: (DEVICE_COOKIE, "DZIENNIKSID", "SDZIENNIKSID"),
}

# Observed live (2026-09-05): the `oauth_token` session cookie is valid for
# ~24h from login. Rather than parsing the exact Set-Cookie expiry out of
# aiohttp's cookie jar (fragile across Max-Age vs. Expires attribute
# quirks), the client tracks elapsed time since its own last successful
# login and assumes this conservative fixed lifetime.
ASSUMED_SESSION_LIFETIME_SECONDS = 20 * 3600
SESSION_EXPIRY_SAFETY_MARGIN_SECONDS = 300

ENDPOINT_ME = "Me"
ENDPOINT_GRADES = "Grades"
ENDPOINT_GRADE_CATEGORIES = "Grades/Categories"
ENDPOINT_NOTES = "Notes"
ENDPOINT_ATTENDANCES = "Attendances"
# CONFIRMED live (2026-09-05): the guessed top-level "AttendanceTypes" 404s;
# the real path is nested under Attendances (same pattern as
# Grades/Categories), matching the `Attendances\Types\<id>` Url seen on
# every Attendances[].Type. Response root key is "Types", not
# "AttendanceTypes" - see coordinator.py's _parse_attendance_types.
ENDPOINT_ATTENDANCE_TYPES = "Attendances/Types"
ENDPOINT_TIMETABLES = "Timetables"
ENDPOINT_HOMEWORKS = "HomeWorks"
ENDPOINT_SCHOOL_NOTICES = "SchoolNotices"
ENDPOINT_LUCKY_NUMBERS = "LuckyNumbers"

# CONFIRMED live (2026-09-05) via scripts/manual_smoke_test.py. Kept
# non-fatal in the coordinator anyway (falls back to raw numeric ids) since
# a different school setup could still vary - see RustySnek/librus-apix's
# README on that point.
ENDPOINT_SUBJECTS = "Subjects"
ENDPOINT_TEACHERS = "Users"
ENDPOINT_CLASSROOMS = "Classrooms"

# UNVERIFIED - distinct from HomeWorks (the general agenda/events feed,
# confirmed working). szkolny-android has this implemented then disabled
# upstream for unknown reliability reasons - probe live before trusting it.
ENDPOINT_HOMEWORK_ASSIGNMENTS = "HomeWorkAssignments"

# CONFIRMED live (2026-09-05), all real and reachable via a normal
# parent/student login - see scripts/manual_smoke_test.py. TeacherFreeDays
# and Substitutions were ALSO tried and both 403 for this account type
# (likely staff/teacher-only permissions) - not included here.
ENDPOINT_SCHOOLS = "Schools"
ENDPOINT_CLASSES = "Classes"
ENDPOINT_VIRTUAL_CLASSES = "VirtualClasses"
ENDPOINT_SCHOOL_FREE_DAYS = "SchoolFreeDays"
ENDPOINT_CLASS_FREE_DAYS = "ClassFreeDays"
ENDPOINT_HOMEWORK_CATEGORIES = "HomeWorks/Categories"
ENDPOINT_PARENT_TEACHER_CONFERENCES = "ParentTeacherConferences"
ENDPOINT_GRADE_TYPES = "Grades/Types"

# CONFIRMED live (2026-09-06), found by reading szkolny-eu/szkolny-android's
# full LibrusApi*.kt file list (not just the higher-level LibrusFeatures.kt
# flags used for the previous round) - see CLAUDE.md's session note.
#
# Notes/Categories: CONFIRMED real+POPULATED (8 real category names on this
# account) - wired into the coordinator immediately, see
# _cached_note_categories.
#
# Everything else below is CONFIRMED real+reachable but returned EMPTY on
# this account - same "confirmed but nothing to build a parser against yet"
# treatment as VirtualClasses/ParentTeacherConferences/HomeWorkAssignments.
# Client methods exist for probing; none are wired into the coordinator.
ENDPOINT_NOTE_CATEGORIES = "Notes/Categories"
# "Ocena zachowania" (a formal behaviour grade, e.g. wzorowe/bardzo dobre) -
# genuinely distinct from Notes ("uwagi", free-text remarks). This school's
# BehaviourGradesSettings (see Units) confirms it uses a points-based
# variant of this.
ENDPOINT_BEHAVIOUR_GRADES_POINTS = "BehaviourGrades/Points"
ENDPOINT_BEHAVIOUR_GRADES_POINTS_CATEGORIES = "BehaviourGrades/Points/Categories"
# CONFIRMED to be a SEPARATE endpoint from /Grades, not embedded per-grade
# data - coordinator.py's _parse_grades currently assumes comments arrive
# nested inside each /Grades item (item["Comments"]), which this endpoint's
# existence casts real doubt on. Flagged, not yet fixed - both are empty on
# this account so there's no real comment record to check the correlation
# key against (probably a nested Grade: {Id: ...} per comment, but
# unconfirmed).
ENDPOINT_GRADE_COMMENTS = "Grades/Comments"
# School/unit configuration - which grade systems are enabled
# (GradesSettings.{Standard,Point,Descriptive}GradesEnabled), bell schedule,
# behaviour-points settings. CONFIRMED this account's school has
# PointGradesEnabled=false but DescriptiveGradesEnabled=true.
ENDPOINT_UNITS = "Units"
# Alternate grading systems, alongside the numeric one this integration
# already supports. PointGrades is confirmed NOT enabled for this account's
# school (see Units above) - kept here for completeness/other schools.
ENDPOINT_POINT_GRADES = "PointGrades"
ENDPOINT_DESCRIPTIVE_GRADES = "DescriptiveGrades"
ENDPOINT_TEXT_GRADES = "TextGrades"

# Wiadomości (private messages) is a SEPARATE subsystem on its own domain,
# reachable only after bootstrapping a dedicated session cookie on top of
# the main Synergia one (reverse-engineered from `emsi/librus_pyapi`, MIT).
MESSAGES_DOMAIN = "wiadomosci.librus.pl"
MESSAGES_BOOTSTRAP_URL = "https://synergia.librus.pl/wiadomosci3"
MESSAGES_BASE_URL = f"https://{MESSAGES_DOMAIN}/api"
# Seen in the bootstrap response body when this Librus module isn't enabled
# for the account - not every school turns it on.
MESSAGES_ACCESS_DENIED_MARKER = "Brak dostępu"
