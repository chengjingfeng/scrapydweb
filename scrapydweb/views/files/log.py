# coding: utf-8
from collections import OrderedDict
from datetime import date, datetime
import io
import json
import os
import re
# from socket import gethostname
from subprocess import Popen
import sys
import tarfile
import time

from flask import flash, render_template, request, url_for
from logparser import parse

from ...vars import CWD as root_dir
from ..myview import MyView


EMAIL_CONTENT_KEYS = [
    'log_critical_count',
    'log_error_count',
    'log_warning_count',
    'log_redirect_count',
    'log_retry_count',
    'log_ignore_count',
    'crawled_pages',
    'scraped_items'
]
job_data_dict = {}
# job_finished_set would only be updated by poll POST with ?job_finished=True > email_notice(),
# used for determining whether to show 'click to refresh' button in the Log and Stats page.
job_finished_set = set()


# http://flask.pocoo.org/docs/1.0/api/#flask.views.View
# http://flask.pocoo.org/docs/1.0/views/
class LogView(MyView):
    job_data_dict = job_data_dict
    job_finished_set = job_finished_set

    def __init__(self):
        super(LogView, self).__init__()  # super().__init__()

        self.opt = self.view_args['opt']
        self.project = self.view_args['project']
        self.spider = self.view_args['spider']
        self.job = self.view_args['job']

        self.job_key = '/%s/%s/%s/%s' % (self.node, self.project, self.spider, self.job)

        # Note that self.SCRAPYD_LOGS_DIR may be an empty string
        # Extension like '.log' is excluded here.
        self.url = u'http://{}/logs/{}/{}/{}'.format(self.SCRAPYD_SERVER, self.project, self.spider, self.job)
        self.log_path = os.path.join(self.SCRAPYD_LOGS_DIR, self.project, self.spider, self.job)

        # For Log and Stats buttons in the Logs page: /a.log/?with_ext=True
        self.with_ext = request.args.get('with_ext', None)
        if self.with_ext:
            self.SCRAPYD_LOG_EXTENSIONS = ['']
            if self.job.endswith('.tar.gz'):
                job_without_ext = self.job[:-len('.tar.gz')]
            else:
                job_without_ext = os.path.splitext(self.job)[0]  # '1.1.log' => ('1.1', '.log')
        else:
            job_without_ext = self.job

        # json file by LogParser
        self.json_path = os.path.join(self.SCRAPYD_LOGS_DIR, self.project, self.spider, job_without_ext + '.json')
        self.json_url = u'http://{}/logs/{}/{}/{}.json'.format(self.SCRAPYD_SERVER, self.project, self.spider,
                                                               job_without_ext)

        self.status_code = 0
        self.text = ''
        self.template = 'scrapydweb/%s%s.html' % (self.opt, '_mobileui' if self.USE_MOBILEUI else '')
        self.kwargs = dict(node=self.node, project=self.project, spider=self.spider,
                           job=job_without_ext, url_refresh='', url_jump='')

        # Request that comes from poll POST for finished job and links of finished job in the Jobs page
        # would be attached with the query string '?job_finished=True'
        self.job_finished = request.args.get('job_finished', None)

        if self.opt == 'utf8':
            flash("It's recommended to check out the latest log via: the Stats page >> View log >> Tail", self.WARN)
            self.utf8_realtime = True
            self.stats_realtime = False
            self.stats_logparser = False
        else:
            self.utf8_realtime = False
            self.stats_realtime = True if request.args.get('realtime', None) else False
            self.stats_logparser = not self.stats_realtime
        self.logparser_valid = False
        self.backup_stats_valid = False
        spider_path = self.mkdir_spider_path()
        self.backup_stats_path = os.path.join(spider_path, job_without_ext + '.json')
        self.stats = {}

        # job_data for email notice: ([0] * 8, [False] * 6, False, time.time())
        self.job_stats_previous = []
        self.triggered_list = []
        self.has_been_stopped = False
        self.last_send_timestamp = 0
        self.job_stats = []
        self.job_stats_diff = []
        self.email_content_kwargs = {}
        self.flag = ''

    def dispatch_request(self, **kwargs):
        # Try to request stats by LogParser to avoid reading/requesting the whole log
        if self.stats_logparser:
            if self.IS_LOCAL_SCRAPYD_SERVER and self.SCRAPYD_LOGS_DIR:
                self.read_local_stats_by_logparser()
            if not self.logparser_valid:
                self.request_stats_by_logparser()

        if not self.logparser_valid and not self.text:
            # Try to read local logfile
            if self.IS_LOCAL_SCRAPYD_SERVER and self.SCRAPYD_LOGS_DIR:
                self.read_local_scrapy_log()
            # Has to request scrapy logfile
            if not self.text:
                self.request_scrapy_log()
                if self.status_code != 200:
                    if self.stats_logparser:
                        self.load_backup_stats()
                    if not self.backup_stats_valid:
                        kwargs = dict(node=self.node, url=self.url, status_code=self.status_code, text=self.text)
                        return render_template(self.template_fail, **kwargs)
            else:
                self.url += self.SCRAPYD_LOG_EXTENSIONS[0]
        else:
            self.url += self.SCRAPYD_LOG_EXTENSIONS[0]

        self.update_kwargs()

        if self.ENABLE_EMAIL and self.POST:  # Only poll.py would make POST request
            self.email_notice()

        return render_template(self.template, **self.kwargs)

    def read_local_stats_by_logparser(self):
        self.logger.debug("Try to read local stats by LogParser: %s", self.json_path)
        try:
            with io.open(self.json_path, 'r', encoding='utf-8') as f:
                js = json.loads(f.read())
        except Exception as err:
            self.logger.error("Fail to read local stats from %s: %s", self.json_path, err)
            return
        else:
            if js.get('logparser_version') != self.LOGPARSER_VERSION:
                msg = "Mismatching logparser_version %s in local stats" % js.get('logparser_version')
                self.logger.warning(msg)
                flash(msg, self.WARN)
                return
            self.logparser_valid = True
            self.stats = js
            msg = "Using local stats: LogParser v%s, last updated at %s, %s" % (
                js['logparser_version'], js['last_update_time'], self.handle_slash(self.json_path))
            self.logger.info(msg)
            flash(msg, self.INFO)

    def request_stats_by_logparser(self):
        self.logger.debug("Try to request stats by LogParser: %s", self.json_url)
        # self.make_request() would check the value of key 'status' if as_json=True
        status_code, js = self.make_request(self.json_url, auth=self.AUTH, as_json=True, dumps_json=False)
        if status_code != 200:
            self.logger.error("Fail to request stats from %s, got status_code: %s", self.json_url, status_code)
            if self.IS_LOCAL_SCRAPYD_SERVER and self.ENABLE_LOGPARSER:
                flash("Request to %s got code %s, wait until LogParser parses the log. " % (self.json_url, status_code),
                      self.INFO)
            else:
                flash(("'pip install logparser' on host '%s' and run command 'logparser'. "
                       "Or wait until LogParser parses the log. ") % self.SCRAPYD_SERVER, self.WARN)
            return
        elif js.get('logparser_version') != self.LOGPARSER_VERSION:
            msg = "'pip install -U logparser' on host '%s' to update LogParser to v%s" % (
                self.SCRAPYD_SERVER, self.LOGPARSER_VERSION)
            self.logger.warning(msg)
            flash(msg, self.WARN)
            return
        else:
            self.logparser_valid = True
            # TODO: dirty data
            self.stats = js
            msg = "LogParser v%s, last updated at %s, %s" % (
                js['logparser_version'], js['last_update_time'], self.json_url)
            self.logger.info(msg)
            flash(msg, self.INFO)

    def read_local_scrapy_log(self):
        for ext in self.SCRAPYD_LOG_EXTENSIONS:
            log_path = self.log_path + ext
            if os.path.exists(log_path):
                if tarfile.is_tarfile(log_path):
                    self.logger.debug("Ignore local tarfile and use requests instead: %s", log_path)
                    break
                with io.open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                    self.text = f.read()
                log_path = self.handle_slash(log_path)
                msg = "Using local logfile: %s" % log_path
                self.logger.debug(msg)
                flash(msg, self.INFO)
                break

    def request_scrapy_log(self):
        for ext in self.SCRAPYD_LOG_EXTENSIONS:
            url = self.url + ext
            self.status_code, self.text = self.make_request(url, auth=self.AUTH, as_json=False)
            if self.status_code == 200:
                self.url = url
                self.logger.debug("Got logfile from %s", self.url)
                break
        else:
            msg = "Fail to request logfile from %s with extensions %s" % (self.url, self.SCRAPYD_LOG_EXTENSIONS)
            self.logger.error(msg)
            flash(msg, self.WARN)
            self.url += self.SCRAPYD_LOG_EXTENSIONS[0]

    def mkdir_spider_path(self):
        node_path = os.path.join(self.STATS_PATH,
                                 re.sub(self.LEGAL_NAME_PATTERN, '-', re.sub(r'[.:]', '_', self.SCRAPYD_SERVER)))
        project_path = os.path.join(node_path, self.project)
        spider_path = os.path.join(project_path, self.spider)

        if not os.path.isdir(self.STATS_PATH):
            os.mkdir(self.STATS_PATH)
        if not os.path.isdir(node_path):
            os.mkdir(node_path)
        if not os.path.isdir(project_path):
            os.mkdir(project_path)
        if not os.path.isdir(spider_path):
            os.mkdir(spider_path)
        return spider_path

    def backup_stats(self):
        # TODO: delete backup stats json file when the job is deleted in the Jobs page with database view
        try:
            with io.open(self.backup_stats_path, 'w', encoding='utf-8', errors='ignore') as f:
                f.write(self.json_dumps(self.stats))
        except Exception as err:
            self.logger.error("Fail to backup stats to %s: %s" % (self.backup_stats_path, err))
            try:
                os.remove(self.backup_stats_path)
            except:
                pass
        else:
            self.logger.info("Saved backup stats to %s", self.backup_stats_path)

    def load_backup_stats(self):
        self.logger.debug("Try to load backup stats by LogParser: %s", self.json_path)
        try:
            with io.open(self.backup_stats_path, 'r', encoding='utf-8') as f:
                js = json.loads(f.read())
        except Exception as err:
            self.logger.error("Fail to load backup stats from %s: %s", self.backup_stats_path, err)
        else:
            if js.get('logparser_version') != self.LOGPARSER_VERSION:
                msg = "Mismatching logparser_version %s in backup stats" % js.get('logparser_version')
                self.logger.warning(msg)
                flash(msg, self.WARN)
                return
            self.logparser_valid = True
            self.backup_stats_valid = True
            self.stats = js
            msg = "Using backup stats: LogParser v%s, last updated at %s, %s" % (
                js['logparser_version'], js['last_update_time'], self.handle_slash(self.backup_stats_path))
            self.logger.info(msg)
            flash(msg, self.WARN)

    @staticmethod
    def get_ordered_dict(adict):
        # 'source', 'last_update_time', 'last_update_timestamp', other keys in order
        odict = OrderedDict()
        for k in ['source', 'last_update_time', 'last_update_timestamp']:
            odict[k] = adict.pop(k)
        for k in sorted(adict.keys()):
            odict[k] = adict[k]
        return odict

    def update_kwargs(self):
        if self.utf8_realtime:
            self.kwargs['text'] = self.text
            self.kwargs['last_update_timestamp'] = time.time()
            if self.job_finished or self.job_key in self.job_finished_set:
                self.kwargs['url_refresh'] = ''
            else:
                self.kwargs['url_refresh'] = 'javascript:location.reload(true);'
        else:
            # Parsed data comes from json.loads, for compatibility with Python 2,
            # use str(time_) to avoid [u'2019-01-01 00:00:01', 0, 0, 0, 0] in JavaScript.
            if self.logparser_valid:
                for d in self.stats['datas']:
                    d[0] = str(d[0])
            else:
                self.logger.warning('Parse the whole log')
                self.stats = parse(self.text)
                # Note that the crawler_engine is not available when using parse()
                self.stats['crawler_engine'] = {}
            # For sorted orders in stats.html with Python 2
            for k in ['crawler_stats', 'crawler_engine']:
                if self.stats[k]:
                    self.stats[k] = self.get_ordered_dict(self.stats[k])

            if self.BACKUP_STATS_JSON_FILE:
                self.backup_stats()
            self.kwargs.update(self.stats)

            if (self.kwargs['finish_reason'] == self.NA
               and not self.job_finished
               and self.job_key not in self.job_finished_set):
                # http://flask.pocoo.org/docs/1.0/api/#flask.Request.url_root
                # _query_string = '?ui=mobile'
                # self.url_refresh = request.script_root + request.path + _query_string
                self.kwargs['url_refresh'] = 'javascript:location.reload(true);'
            if self.kwargs['url_refresh']:
                if self.stats_logparser and not self.logparser_valid:
                    self.kwargs['url_jump'] = ''
                else:
                    self.kwargs['url_jump'] = url_for('log', node=self.node, opt='stats', project=self.project,
                                                      spider=self.spider, job=self.job, with_ext=self.with_ext,
                                                      ui=self.UI, realtime='True' if self.stats_logparser else None)

        # Stats link of 'a.json' from the Logs page should hide these links
        if self.with_ext and self.job.endswith('.json'):
            self.kwargs['url_source'] = ''
            self.kwargs['url_opt_opposite'] = ''
            self.kwargs['url_refresh'] = ''
            self.kwargs['url_jump'] = ''
        else:
            self.kwargs['url_source'] = self.url
            self.kwargs['url_opt_opposite'] = url_for('log', node=self.node,
                                                      opt='utf8' if self.opt == 'stats' else 'stats',
                                                      project=self.project, spider=self.spider, job=self.job,
                                                      job_finished=self.job_finished, with_ext=self.with_ext,
                                                      ui=self.UI)

    def email_notice(self):
        job_data_default = ([0] * 8, [False] * 6, False, time.time())
        job_data = self.job_data_dict.setdefault(self.job_key, job_data_default)
        (self.job_stats_previous, self.triggered_list, self.has_been_stopped, self.last_send_timestamp) = job_data
        self.logger.info(self.job_data_dict)
        self.job_stats = [self.kwargs['log_categories'][k.lower() + '_logs']['count']
                          for k in self.EMAIL_TRIGGER_KEYS]
        self.job_stats.extend([self.kwargs['pages'] or 0, self.kwargs['items'] or 0])  # May be None by LogParser
        self.job_stats_diff = [j - i for i, j in zip(self.job_stats_previous, self.job_stats)]

        self.set_email_content_kwargs()
        self.set_email_flag()
        self.handle_email_flag()

    def set_email_content_kwargs(self):
        # For compatibility with Python 2, use OrderedDict() to keep insertion order
        self.email_content_kwargs = OrderedDict()
        self.email_content_kwargs['SCRAPYD_SERVER'] = self.SCRAPYD_SERVER
        # self.email_content_kwargs['hostname'] = gethostname()
        self.email_content_kwargs['project'] = self.kwargs['project']
        self.email_content_kwargs['spider'] = self.kwargs['spider']
        self.email_content_kwargs['job'] = self.kwargs['job']
        self.email_content_kwargs['first_log_time'] = self.kwargs['first_log_time']
        self.email_content_kwargs['latest_log_time'] = self.kwargs['latest_log_time']
        self.email_content_kwargs['runtime'] = self.kwargs['runtime']
        self.email_content_kwargs['shutdown_reason'] = self.kwargs['shutdown_reason']
        self.email_content_kwargs['finish_reason'] = self.kwargs['finish_reason']
        self.email_content_kwargs['url_stats'] = request.url + '%sui=mobile' % '&' if request.args else '?'

        for idx, key in enumerate(EMAIL_CONTENT_KEYS):
            if self.job_stats_diff[idx]:
                self.email_content_kwargs[key] = '%s + %s' % (self.job_stats_previous[idx], self.job_stats_diff[idx])
            else:
                self.email_content_kwargs[key] = self.job_stats[idx]
        # pages and items may be None by LogParser
        if self.kwargs['pages'] is None:
            self.email_content_kwargs['crawled_pages'] = self.NA
        if self.kwargs['items'] is None:
            self.email_content_kwargs['scraped_items'] = self.NA

        _url_stop = url_for('api', node=self.node, opt='stop', project=self.project, version_spider_job=self.job)
        self.email_content_kwargs['url_stop'] = self.URL_SCRAPYDWEB + _url_stop

        now_timestamp = time.time()
        for k in ['latest_crawl', 'latest_scrape', 'latest_log']:
            ts = self.kwargs['%s_timestamp' % k]
            self.email_content_kwargs[k] = self.NA if ts == 0 else "%s secs ago" % int(now_timestamp - ts)

        self.email_content_kwargs['current_time'] = self.get_now_string(True)
        self.email_content_kwargs['logparser_version'] = self.kwargs['logparser_version']
        self.email_content_kwargs['latest_item'] = self.kwargs['latest_matches']['latest_item'] or self.NA
        self.email_content_kwargs['Crawler.stats'] = self.kwargs['crawler_stats']
        self.email_content_kwargs['Crawler.engine'] = self.kwargs['crawler_engine']

    def set_email_flag(self):
        if self.ON_JOB_FINISHED and self.job_finished:
            self.flag = 'Finished'
        elif not all(self.triggered_list):
            to_forcestop = False
            to_stop = False
            # The order of the elements in EMAIL_TRIGGER_KEYS matters:
            # ['CRITICAL', 'ERROR', 'WARNING', 'REDIRECT', 'RETRY', 'IGNORE']
            for idx, key in enumerate(self.EMAIL_TRIGGER_KEYS):
                if (0 < getattr(self, 'LOG_%s_THRESHOLD' % key, 0) <= self.job_stats[idx]
                   and not self.triggered_list[idx]):
                    self.triggered_list[idx] = True
                    self.email_content_kwargs['log_%s_count' % key.lower()] += ' triggered!!!'
                    if getattr(self, 'LOG_%s_TRIGGER_FORCESTOP' % key):
                        self.flag = '%s_ForceStop' % key if '_ForceStop' not in self.flag else self.flag
                        to_forcestop = True
                    elif getattr(self, 'LOG_%s_TRIGGER_STOP' % key) and not self.has_been_stopped:
                        self.flag = '%s_Stop' % key if 'Stop' not in self.flag else self.flag
                        self.has_been_stopped = True  # Execute 'Stop' one time at most to avoid unclean shutdown
                        to_stop = True
                    elif not self.has_been_stopped:
                        self.flag = '%s_Trigger' % key if not self.flag else self.flag
            if to_forcestop:
                self.logger.debug("%s: %s", self.flag, self.job_key)
                # api(self.node, 'forcestop', self.project, self.job)
                _url = url_for('api', node=self.node, opt='forcestop',
                               project=self.project, version_spider_job=self.job)
                self.get_response_from_view(_url)
            elif to_stop:
                self.logger.debug("%s: %s", self.flag, self.job_key)
                # api(self.node, 'stop', self.project, self.job)
                _url = url_for('api', node=self.node, opt='stop',
                               project=self.project, version_spider_job=self.job)
                self.get_response_from_view(_url)

        if not self.flag and 0 < self.ON_JOB_RUNNING_INTERVAL <= time.time() - self.last_send_timestamp:
            self.flag = 'Running'

    def handle_email_flag(self):
        if self.flag:
            # Send email
            # now_day = date.isoweekday(datetime.now())
            now_day = date.isoweekday(date.today())
            now_hour = datetime.now().hour
            if now_day in self.EMAIL_WORKING_DAYS and now_hour in self.EMAIL_WORKING_HOURS:
                kwargs = dict(
                    flag=self.flag,
                    pages=self.NA if self.kwargs['pages'] is None else self.kwargs['pages'],
                    items=self.NA if self.kwargs['items'] is None else self.kwargs['items'],
                    job_key=self.job_key,
                    latest_item=self.kwargs['latest_matches']['latest_item'][:100] or self.NA
                )
                subject = u"{flag} [{pages}p, {items}i] {job_key} {latest_item} #scrapydweb".format(**kwargs)
                self.EMAIL_KWARGS['subject'] = subject
                self.EMAIL_KWARGS['content'] = self.json_dumps(self.email_content_kwargs, sort_keys=False)

                args = [
                    sys.executable,
                    os.path.join(root_dir, 'utils', 'send_email.py'),
                    self.json_dumps(self.EMAIL_KWARGS, ensure_ascii=True)
                ]
                self.logger.info("Sending email: %s", self.EMAIL_KWARGS['subject'])
                Popen(args)

            # Update self.job_data_dict (last_send_timestamp would be updated only when flag is non-empty)
            self.logger.info("Previous job_data['%s'] %s", self.job_key, self.job_data_dict[self.job_key])
            self.job_data_dict[self.job_key] = (self.job_stats, self.triggered_list, self.has_been_stopped, time.time())
            self.logger.info("Updated  job_data['%s'] %s", self.job_key, self.job_data_dict[self.job_key])

        if self.job_finished:
            self.job_data_dict.pop(self.job_key)
            if len(self.job_finished_set) > 1000:
                self.job_finished_set.clear()
            self.job_finished_set.add(self.job_key)
            self.logger.info('job_finished: %s', self.job_key)
