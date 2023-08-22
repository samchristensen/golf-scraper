from collections import defaultdict
import os
from typing import Dict, List
from redis import Redis
import requests
import datetime
from datetime import timedelta
import abc
from pytz import timezone

eastern = timezone("US/Eastern")


class ForeupCourses(abc.ABC):
    @abc.abstractproperty
    def course_name(self) -> str:
        pass

    @abc.abstractproperty
    def booking_url(self) -> str:
        pass

    @abc.abstractproperty
    def schedule_id(self) -> str:
        pass

    @abc.abstractproperty
    def booking_class(self) -> str:
        pass

    @abc.abstractproperty
    def schedule_ids(self) -> List[str]:
        pass


class Bethpage(ForeupCourses):
    course_name = "Bethpage"
    booking_url = "https://foreupsoftware.com/index.php/booking/19765/2431#teetimes"
    schedule_id = "2431"
    booking_class = "2137"
    schedule_ids = (["2517", "2431", "2433", "2539", "2538", "2434", "2432", "2435"],)


class GraniteLinks(ForeupCourses):
    course_name = "Granite Links"
    booking_url = "https://foreupsoftware.com/index.php/booking/21747/8766#teetimes"
    schedule_id = "8766"
    booking_class = "11167"
    schedule_ids = ["8766"]


course = (
    Bethpage if os.environ.get("COURSE", "BETHPAGE") == "BETHPAGE" else GraniteLinks
)

slack_url = os.environ["SLACK_URL"]
days = int(os.environ.get("DAYS", 7))
redis = Redis(
    host=os.environ["REDIS_HOST"],
    port=int(os.environ["REDIS_PORT"]),
    decode_responses=True,
)

session = requests.Session()

cache_key = f"foreup:{course.course_name}"
attempts_key = f"foreup:{course.course_name}:attempts"
last_checkin_key = f"foreup:{course.course_name}:last_checkin"


class ForeupSoftware:
    base_url: str = "https://foreupsoftware.com/index.php/api/booking/times"
    course: ForeupCourses

    def __init__(self, course: ForeupCourses):
        self.course = course

    def get_tee_times(self, date: datetime.date) -> Dict:
        response = session.get(
            self.base_url,
            params=dict(
                booking_class=self.course.booking_class,
                schedule_id=self.course.schedule_id,
                schedule_ids=self.course.schedule_ids,
                specials_only=0,
                api_key="no_limits",
                time="all",
                date=date.strftime("%m-%d-%Y"),
                holes="all",
                players=0,
            ),
        )

        response.raise_for_status()
        return response.json()


def create_message(message: str):
    return f"""
<@U05NZ767141> <@U05PA8QEPQQ> <@U05PA91SF08>

{message}
        """


try:
    target_date = datetime.datetime.strptime(os.environ["TARGET_DATE"], "%Y-%m-%d")
except Exception as e:
    print(e)
    target_date = None

if not target_date:
    tee_times = [
        tee_time
        for i in range(days)
        for tee_time in ForeupSoftware(course).get_tee_times(
            datetime.date.today() + timedelta(days=int(i))
        )
    ]
else:
    print(f"looking on target date {target_date}")
    tee_times = [
        tee_time for tee_time in ForeupSoftware(course).get_tee_times(target_date)
    ]


tee_times_by_day = defaultdict(list)
for tee_time in tee_times:
    _tee_time = datetime.datetime.strptime(
        tee_time["time"], "%Y-%m-%d %H:%M"
    ).astimezone(eastern)
    if tee_time["available_spots"] < 3 or _tee_time.hour >= 16:
        print(
            "Skipping because it's too late or has too few spots: "
            f"{tee_time['available_spots']} at {_tee_time}"
        )
        continue

    tee_times_by_day[_tee_time.strftime("%Y-%m-%d")].append(
        _tee_time.strftime("%I:%M %p")
    )

times = [f"{', '.join(times)} on {day}" for day, times in tee_times_by_day.items()]
last_checkin = redis.get(last_checkin_key)

try:
    last_checkin = datetime.datetime.fromisoformat(last_checkin).strftime(
        "%I:%M %p on %m-%d"
    )
except TypeError:
    last_checkin = None

if times:
    message = create_message(
        f"Found tee times for {course.course_name}:\n\n"
        "{}\n\n{}".format("\n".join(times), course.booking_url)
    )
elif redis.exists(cache_key):
    redis.incrby(attempts_key, 1)
    ignore_until = datetime.datetime.now() + timedelta(seconds=redis.ttl(cache_key))
    print(
        f"No tee times found, ignoring until {ignore_until.strftime('%I:%M %p on %m-%d')}"
    )
    message = None
else:
    redis.set(cache_key, "1", ex=60 * 60 * 6)
    attempts = int(redis.incrby(attempts_key, 1))

    redis.delete(attempts_key)
    message = create_message(
        f"No tee times found for {course.course_name} after "
        f"{attempts} attempts since {last_checkin}"
    )

if message:
    redis.set(last_checkin_key, datetime.datetime.now().isoformat())
    response = session.post(
        slack_url,
        json={"text": message},
    )
