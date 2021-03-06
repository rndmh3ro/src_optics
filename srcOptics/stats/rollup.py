from django.utils.dateparse import parse_datetime
from django.db.models import Sum, Count, IntegerField
from django.db.models.functions import Cast
from django.conf import settings
from django.utils import timezone
from srcOptics.models import *
from srcOptics.create import Creator
import concurrent.futures
import datetime
import json
from django.db import transaction

intervals =  Statistic.INTERVALS

#
# This class aggregates commit data already scanned in based on intervals
# Intervals include: day (DY), week (WK), and month (MN).
#
class Rollup:

    #Gets the current date in UTC date time
    today = datetime.datetime.now(tz=timezone.utc)

    #Counts the number of files for a given queryset of commits
    def count_files(commits):
        file_total = 0
        for c in commits.iterator():
            files = c.files
            file_total += files.count()
        return file_total

    #Gets the first day of the week or the month depending on intervals
    #Sets the time to 12:00 AM or 00:00 for that day
    def get_first_day(date_index, interval):
        if interval[0] is 'WK':
            date_index -= datetime.timedelta(days=date_index.isoweekday() % 7)
        elif interval[0] is 'MN':
            date_index = date_index.replace(day = 1)
        date_index = date_index.replace(hour=0, minute=0, second=0, microsecond=0)
        return date_index

    #Gets the last day of the week or month depending on INTERVALS
    #Sets the time to 11:59 PM or 23:59 for the day
    def get_end_day(date_index, interval):
        if interval[0] is 'WK':
            date_index = date_index + datetime.timedelta(days=6)
        elif interval[0] is 'MN':
            date_delta = date_index.replace(day = 28) + datetime.timedelta(days = 4)
            date_index = date_delta - datetime.timedelta(days=date_delta.day)

        date_index = date_index.replace(hour=23, minute=59, second=59, microsecond=99)
        return date_index

    # Aggregates the total rollup for the daily interval
    @classmethod
    def aggregate_day_rollup(cls,repo):

        total_instances = []
        date_index = repo.last_scanned

        # Daily rollups aren't dependent on the time
        # This allows us to scan the current day
        while date_index.date() != cls.today.date():

            # enable speeding by rollups already computed in event of ctrl-c or fault
            if (repo.last_rollup is None) or (repo.last_rollup <= date_index):
                date_index = cls.aggregate_day_rollup_internal(repo, total_instances, date_index)
                repo.last_rollup = date_index
                repo.save()
            else:
                date_index += datetime.timedelta(days=1)

        Statistic.objects.bulk_create(total_instances, 5000, ignore_conflicts=True)

        return date_index

    @classmethod
    def aggregate_day_rollup_internal(cls, repo, total_instances, date_index):

        # FIXME: we should be able to check if this already exists and not recalculate it.


        #Filters commits by date_index's day value as well as the repo
        commits = Commit.objects.filter(commit_date__contains=date_index.date(), repo=repo).prefetch_related('files')

        # If there are no commits for the day, continue
        if len(commits) == 0 :
            date_index += datetime.timedelta(days=1)
            return date_index
        print("Aggregate Day: %s, %s" % (repo, date_index))

        #Count the number of files in a commit
        file_total = cls.count_files(commits)

        #Get all the authors for the commits for that day
        authors_for_day = commits.values_list('author')
        author_count = len(set(authors_for_day))

        #Aggregate values from query set for rollup
        data = commits.aggregate(lines_added=Sum("lines_added"), lines_removed = Sum("lines_removed"))
        # FIXME: an object using slots here might be faster
        data['commit_total'] = len(commits)
        data['files_changed'] = file_total
        data['author_total'] = author_count
        #Calculated by adding lines added and removed
        data['lines_changed'] = int(data['lines_added']) + int(data['lines_removed'])

        # Create total rollup row for the day
        Creator.create_total_rollup(start_date=date_index, interval=intervals[0][0], repo=repo, 
            lines_added=data['lines_added'], 
            lines_removed=data['lines_removed'],
            lines_changed=data['lines_changed'], 
            commit_total=data['commit_total'], 
            files_changed=data['files_changed'],
            author_total=data['author_total'], 
            total_instances=total_instances)
        #Increment date_index to the next day
        date_index += datetime.timedelta(days=1)
        return date_index

    # Compile each statistic for each author over every day in the date range
    @classmethod
    def aggregate_author_rollup_day(cls, repo, author):

        #earliest_commit = Commit.objects.filter(repo=repo).earliest("commit_date").commit_date

        date_index = repo.last_scanned
        author_instances = []

        all_dates = [ x.date() for x in Commit.objects.filter(author=author, repo=repo).values_list('commit_date', flat=True).all() ]

        def has_date(which, many):
            for x in many:
                if x == which:
                    return True
            return False

        # Daily rollups aren't dependent on the time
        # This allows us to scan the current day
        while date_index.date() != cls.today.date():

            if not has_date(date_index.date(), all_dates):
                date_index += datetime.timedelta(days=1)
                continue
            else:
                print(" -> %s" % date_index)

            # FIXME: move to debug logging
            # print("Author Rollup by Day: %s, %s, %s" % (repo, date_index, author))

            #Filters commits by author,  date_index's day value, and repo
            commits = Commit.objects.filter(author=author, commit_date__contains=date_index.date(), repo=repo).prefetch_related('files')



            #Count the number of files in a commit
            file_total = cls.count_files(commits)

            #Aggregate values from query set for author rollup
            data = commits.aggregate(lines_added = Sum("lines_added"), lines_removed = Sum("lines_removed"))
            data['commit_total'] = len(commits)
            data['files_changed'] = file_total

            #Calculated by adding lines added and removed
            data['lines_changed'] = int(data['lines_added']) + int(data['lines_removed'])

            # Create author rollup row for the day
            Creator.create_author_rollup(date_index, intervals[0][0], repo, author, data['lines_added'], data['lines_removed'],
            data['lines_changed'], data['commit_total'], data['files_changed'], author_instances)

            #Increment date_index to the next day
            date_index += datetime.timedelta(days=1)

        Statistic.objects.bulk_create(author_instances, 5000, ignore_conflicts=True)

    # Compile each statistic for the author based on the interval over the date range
    @classmethod
    def aggregate_author_rollup(cls, repo, interval):

        date_index = cls.get_first_day(repo.last_scanned, interval)
        author_instances = []

        while date_index < cls.today:

            # FIXME: move to debug logging
            print("Author Rollup Aggregration: repo=%s, interval=%s" % (repo, interval))
            end_date = cls.get_end_day(date_index, interval)


            #Gets all the daily author statistic objects
            #.values parameter groups author objects by author id
            #.annotate sums the relevant statisic and adds a new field to the queryset
            days = Statistic.objects.filter(
                interval='DY',
                author__isnull=False,
                file=None,
                repo=repo,
                start_date__range=(date_index, end_date)
            ).values('author_id').annotate(lines_added_total=Sum("lines_added"), lines_removed_total = Sum("lines_removed"),
                                lines_changed_total = Sum("lines_changed"), commit_total_total = Sum("commit_total"),
                                files_changed_total = Sum("files_changed"), author_total_total = Sum("author_total"))


            #iterate through the query set and create author objects
            author = None
            for d in days:
                author = Author.objects.get(pk=d['author_id'])
                # FIXME: these should use keyword arguments
                Creator.create_author_rollup(date_index, interval[0], repo, author, d['lines_added_total'], d['lines_removed_total'],
                d['lines_changed_total'], d['commit_total_total'], d['files_changed_total'], author_instances)

            #Increment to next week or month
            end_date = end_date + datetime.timedelta(days=1)
            date_index = end_date.replace(hour=0, minute=0, second=0, microsecond=0)

        Statistic.objects.bulk_create(author_instances, 5000, ignore_conflicts=True)


    #Compile rollup for interval by aggregating daily stats
    @classmethod
    def aggregate_interval_rollup(cls, repo, interval):

        # FIXME: move to debug logging
        print("Interval Rollup: %s, %s" % (repo, interval))

        #Gets the first day depending on the interval
        date_index = cls.get_first_day(repo.last_scanned, interval)
        total_instances = []

        while date_index < cls.today:
            end_date = cls.get_end_day(date_index, interval)

            days = Statistic.objects.filter(interval = 'DY', author = None, repo = repo, file = None, start_date__range=(date_index, end_date))

            #Aggregates total stats for the interval
            data = days.aggregate(lines_added=Sum("lines_added"), lines_removed = Sum("lines_removed"),
                                lines_changed = Sum("lines_changed"), commit_total = Sum("commit_total"),
                                files_changed = Sum("files_changed"), author_total = Sum("author_total"))

            #Creates row for given interval
            Creator.create_total_rollup(start_date=date_index, interval=interval[0], repo=repo, 
                lines_added=data['lines_added'], 
                lines_removed=data['lines_removed'],
                lines_changed=data['lines_changed'], 
                commit_total=data['commit_total'], 
                files_changed=data['files_changed'], 
                author_total=data['author_total'], 
                total_instances=total_instances)

            #Increment to next week or month
            end_date = end_date + datetime.timedelta(days=1)
            date_index = end_date.replace(hour=0, minute=0, second=0, microsecond=0)

        Statistic.objects.bulk_create(total_instances, 5000, ignore_conflicts=True)

    #Compile total rollups for all intervals
    @classmethod
    def compile_total_rollup (cls, repo, interval):
        #Compile day using repo data
        if interval[0] is 'DY':
            last_scanned = cls.aggregate_day_rollup(repo)
        #compile other intervals using day stats which have been aggregated
        else:
            last_scanned = cls.aggregate_interval_rollup(repo, interval)

    @classmethod
    def compile_author_rollups_thread (cls, repo, author):
        cls.aggregate_author_rollup_day(repo, author)
        # We need to manually close the database connection here or
        # else Django will leave it dangling
        connection.close()

    #We only want to iterate through the authors once.
    #Currently, this seems like the easiest way to do so
    @classmethod 
    def compile_author_rollups (cls, repo):
        #Need to compile authors separately. Iterator is used for memory efficiency
        authors = Author.objects.filter(repos__in=[repo]).iterator()
        total_authors = Author.objects.filter(repos__in=[repo]).count()

        # Django unit tests can be run as parallel, but this causes problems with
        # lingering postgres connections. use the concurrent flag to disable
        # multi-threaded aggregation so that the unit tests behave
        #
        #   hopefully this will go away eventually
        if settings.MULTITHREAD_AGGREGATE is True:

            # this is currently DISABLED by DEFAULT, because I theorize the connection.close is pretty ineffecient
            # look into connection pool here and generalize this at a higher level? -- MPD

            with concurrent.futures.ThreadPoolExecutor(max_workers=settings.MAX_THREAD_COUNT) as pool:
                for author in authors:
                    pool.submit(cls.compile_author_rollups_thread, repo, author)
            cls.aggregate_author_rollup(repo, intervals[1])
            cls.aggregate_author_rollup(repo, intervals[2])
        else:
            count = 0
            for author in authors:
                count = count + 1
                print("Author %s: %s -> # %s/%s" % (repo, author, count, total_authors))
                cls.aggregate_author_rollup_day(repo, author)
            # FIXME: use specific constants for day and week here.
            cls.aggregate_author_rollup(repo, intervals[1])
            cls.aggregate_author_rollup(repo, intervals[2])

    #Compute rollups for specified repo passed in by daemon
    #TODO: Index commit_date and repo together
    @classmethod
    # FIXME: shouldn't be atomic here probably, so killing halfway through allows resumption
    @transaction.atomic
    def rollup_repo(cls, repo):

        #This means that the repo has not been scanned
        if repo.last_scanned is None:
            #So we set the last scanned field to the earliest commit field
            earliest_commit = Commit.objects.filter(repo=repo).earliest("commit_date").commit_date
            repo.last_scanned = earliest_commit
            repo.earliest_commit = earliest_commit

        for interval in intervals:
            cls.compile_total_rollup(repo, interval)

        cls.compile_author_rollups(repo)

        #Set last scanned date to today
        repo.last_scanned = cls.today
        repo.save()
