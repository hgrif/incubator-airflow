# -*- coding: utf-8 -*-
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import logging
import os
import re
import subprocess
import sys

from airflow.hooks.base_hook import BaseHook
from airflow.exceptions import AirflowException

log = logging.getLogger(__name__)


class SparkSubmitHook(BaseHook):
    """
    This hook is a wrapper around the spark-submit binary to kick off a
    spark-submit job.
    
    It requires that the "spark-submit" binary is in the PATH or the spark_home
    to be supplied.
    
    :param conf: Arbitrary Spark configuration properties
    :type conf: dict
    :param conn_id: The connection id as configured in Airflow administration.
        When an invalid connection_id is supplied, it will default to yarn.
    :type conn_id: str
    :param files: Upload additional files to the container running the job,
        separated by a comma. For example hive-site.xml.
    :type files: str
    :param py_files: Additional python files used by the job, can be .zip, .egg
        or .py.
    :type py_files: str
    :param jars: Submit additional jars to upload and place them in executor
        classpath.
    :type jars: str
    :param java_class: the main class of the Java application
    :type java_class: str
    :param executor_cores: Number of cores per executor (Default: 2)
    :type executor_cores: int
    :param executor_memory: Memory per executor (e.g. 1000M, 2G) (Default: 1G)
    :type executor_memory: str
    :param driver_memory: Memory allocated to the driver (e.g. 1000M, 2G)
        (Default: 1G)
    :type driver_memory: str
    :param keytab: Full path to the file that contains the keytab
    :type keytab: str
    :param principal: The name of the kerberos principal used for keytab
    :type principal: str
    :param name: Name of the job (default airflow-spark)
    :type name: str
    :param num_executors: Number of executors to launch
    :type num_executors: int
    :param verbose: Whether to pass the verbose flag to spark-submit process
        for debugging
    :type verbose: bool
    """

    def __init__(self,
                 conf=None,
                 conn_id='spark_default',
                 files=None,
                 py_files=None,
                 jars=None,
                 java_class=None,
                 executor_cores=None,
                 executor_memory=None,
                 driver_memory=None,
                 keytab=None,
                 principal=None,
                 name='default-name',
                 num_executors=None,
                 verbose=False):
        self._conf = conf
        self._conn_id = conn_id
        self._files = files
        self._py_files = py_files
        self._jars = jars
        self._java_class = java_class
        self._executor_cores = executor_cores
        self._executor_memory = executor_memory
        self._driver_memory = driver_memory
        self._keytab = keytab
        self._principal = principal
        self._name = name
        self._num_executors = num_executors
        self._verbose = verbose
        self._sp = None
        self._yarn_application_id = None

        (self._master, self._queue, self._deploy_mode,
         self._spark_home) = self._resolve_connection()
        self._is_yarn = 'yarn' in self._master

    def _resolve_connection(self):
        # Build from connection master or default to yarn if not available
        master = 'yarn'
        queue = None
        deploy_mode = None
        spark_home = None

        try:
            # Master can be local, yarn, spark://HOST:PORT or mesos://HOST:PORT
            conn = self.get_connection(self._conn_id)
            if conn.port:
                master = "{}:{}".format(conn.host, conn.port)
            else:
                master = conn.host

            # Determine optional yarn queue from the extra field
            extra = conn.extra_dejson
            if 'queue' in extra:
                queue = extra['queue']
            if 'deploy-mode' in extra:
                deploy_mode = extra['deploy-mode']
            if 'spark-home' in extra:
                spark_home = extra['spark-home']
        except AirflowException:
            logging.debug(
                "Could not load connection string {}, defaulting to {}".format(
                    self._conn_id, master
                )
            )

        return master, queue, deploy_mode, spark_home

    def get_conn(self):
        pass

    def _build_command(self, application):
        """
        Construct the spark-submit command to execute.
        :param application: command to append to the spark-submit command
        :type application: str
        :return: full command to be executed
        """
        # If the spark_home is passed then build the spark-submit executable
        # path using the spark_home; otherwise assume that spark-submit is
        # present in the path to the executing user
        if self._spark_home:
            connection_cmd = [os.path.join(self._spark_home, 'bin',
                                           'spark-submit')]
        else:
            connection_cmd = ['spark-submit']

        # The url ot the spark master
        connection_cmd += ["--master", self._master]

        if self._conf:
            for key in self._conf:
                connection_cmd += ["--conf", "{}={}".format(key, str(self._conf[key]))]
        if self._files:
            connection_cmd += ["--files", self._files]
        if self._py_files:
            connection_cmd += ["--py-files", self._py_files]
        if self._jars:
            connection_cmd += ["--jars", self._jars]
        if self._num_executors:
            connection_cmd += ["--num-executors", str(self._num_executors)]
        if self._executor_cores:
            connection_cmd += ["--executor-cores", str(self._executor_cores)]
        if self._executor_memory:
            connection_cmd += ["--executor-memory", self._executor_memory]
        if self._driver_memory:
            connection_cmd += ["--driver-memory", self._driver_memory]
        if self._keytab:
            connection_cmd += ["--keytab", self._keytab]
        if self._principal:
            connection_cmd += ["--principal", self._principal]
        if self._name:
            connection_cmd += ["--name", self._name]
        if self._java_class:
            connection_cmd += ["--class", self._java_class]
        if self._verbose:
            connection_cmd += ["--verbose"]
        if self._queue:
            connection_cmd += ["--queue", self._queue]
        if self._deploy_mode:
            connection_cmd += ["--deploy-mode", self._deploy_mode]

        # The actual script to execute
        connection_cmd += [application]

        logging.debug("Spark-Submit cmd: {}".format(connection_cmd))

        return connection_cmd

    def submit(self, application="", **kwargs):
        """
        Remote Popen to execute the spark-submit job

        :param application: Submitted application, jar or py file
        :type application: str
        :param kwargs: extra arguments to Popen (see subprocess.Popen)
        """
        spark_submit_cmd = self._build_command(application)
        # Stream stdout while also piping stderr into stdout.
        # See http://stackoverflow.com/a/17698359.
        if sys.version_info.major >= 3:
            with subprocess.Popen(spark_submit_cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, bufsize=1,
                                  universal_newlines=True, **kwargs) as p:
                for line in p.stdout:
                    self._process_log(line)
                self._sp = p
        else:
            self._sp = subprocess.Popen(
                spark_submit_cmd, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=1, **kwargs
            )
            with self._sp.stdout as out:
                for line in iter(out.readline, b''):
                    self._process_log(line)
            self._sp.wait()  # wait for the subprocess to exit
        if self._sp.returncode:
            raise AirflowException(
                "Cannot execute: {cmd}. Error code is: {code}".format(
                    cmd=spark_submit_cmd, code=self._sp.returncode
                )
            )

    def _process_log(self, itr):
        """
        Processes the log files and extracts useful information out of it

        :param itr: An iterator which iterates over the input of the subprocess
        """
        for line in itr:
            line = line.strip()
            # If we run yarn cluster mode, we want to extract the application
            # id from the itr so we can kill the application when we stop it
            # unexpectedly
            if self._is_yarn and self._deploy_mode == 'cluster':
                match = re.search('(application[0-9_]+)', line)
                if match:
                    self._yarn_application_id = match.groups()[0]
            # Pass to logging
            logging.info(line)

    def on_kill(self):
        if self._sp and self._sp.poll() is None:
            logging.info('Sending kill signal to spark-submit')
            self._sp.kill()

            if self._yarn_application_id:
                logging.info('Killing application on YARN')
                try:
                    kill_cmd = "yarn application -kill {0}"
                    subprocess.check_call(
                        kill_cmd.format(self._yarn_application_id),
                    )
                    logging.info("YARN successfully killed.")
                except subprocess.CalledProcessError as e:
                    msg = "YARN killed with return code: {0}"
                    logging.info(msg.format(e.returncode))
