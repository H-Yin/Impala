# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
# Client tests for SQL statement authorization

import os
import pytest
import shutil
import tempfile
import json
import grp
import re
import sys
import subprocess
import urllib

from time import sleep, time
from getpass import getuser
from ImpalaService import ImpalaHiveServer2Service
from TCLIService import TCLIService
from thrift.transport.TSocket import TSocket
from thrift.transport.TTransport import TBufferedTransport
from thrift.protocol import TBinaryProtocol
from tests.common.custom_cluster_test_suite import CustomClusterTestSuite
from tests.common.file_utils import assert_file_in_dir_contains,\
    assert_no_files_in_dir_contain
from tests.hs2.hs2_test_suite import operation_id_to_query_id
from tests.util.filesystem_utils import WAREHOUSE

AUTH_POLICY_FILE = "%s/authz-policy.ini" % WAREHOUSE
SENTRY_CONFIG_DIR = os.getenv('IMPALA_HOME') + '/fe/src/test/resources/'
SENTRY_BASE_LOG_DIR = os.getenv('IMPALA_CLUSTER_LOGS_DIR') + "/sentry"
SENTRY_CONFIG_FILE = SENTRY_CONFIG_DIR + 'sentry-site.xml'
SENTRY_CONFIG_FILE_OO = SENTRY_CONFIG_DIR + 'sentry-site_oo.xml'
PRIVILEGES = ['all', 'alter', 'drop', 'insert', 'refresh', 'select']
ADMIN = "admin"

class TestAuthorization(CustomClusterTestSuite):
  AUDIT_LOG_DIR = tempfile.mkdtemp(dir=os.getenv('LOG_DIR'))

  def setup(self):
    host, port = (self.cluster.impalads[0].service.hostname,
                  self.cluster.impalads[0].service.hs2_port)
    self.socket = TSocket(host, port)
    self.transport = TBufferedTransport(self.socket)
    self.transport.open()
    self.protocol = TBinaryProtocol.TBinaryProtocol(self.transport)
    self.hs2_client = ImpalaHiveServer2Service.Client(self.protocol)

  def teardown(self):
    if self.socket:
      self.socket.close()
    shutil.rmtree(self.AUDIT_LOG_DIR, ignore_errors=True)

  @pytest.mark.execute_serially
  @CustomClusterTestSuite.with_args("--server_name=server1\
      --authorization_policy_file=%s\
      --authorization_policy_provider_class=%s" %\
      (AUTH_POLICY_FILE,
       "org.apache.sentry.provider.file.LocalGroupResourceAuthorizationProvider"))
  def test_custom_authorization_provider(self):
    from tests.hs2.test_hs2 import TestHS2
    open_session_req = TCLIService.TOpenSessionReq()
    # User is 'test_user' (defined in the authorization policy file)
    open_session_req.username = 'test_user'
    open_session_req.configuration = dict()
    resp = self.hs2_client.OpenSession(open_session_req)
    TestHS2.check_response(resp)

    # Try to query a table we are not authorized to access.
    self.session_handle = resp.sessionHandle
    execute_statement_req = TCLIService.TExecuteStatementReq()
    execute_statement_req.sessionHandle = self.session_handle
    execute_statement_req.statement = "describe tpch_seq.lineitem"
    execute_statement_resp = self.hs2_client.ExecuteStatement(execute_statement_req)
    assert 'User \'%s\' does not have privileges to access' % 'test_user' in\
        str(execute_statement_resp)

    # Now try the same operation on a table we are authorized to access.
    execute_statement_req = TCLIService.TExecuteStatementReq()
    execute_statement_req.sessionHandle = self.session_handle
    execute_statement_req.statement = "describe tpch.lineitem"
    execute_statement_resp = self.hs2_client.ExecuteStatement(execute_statement_req)
    TestHS2.check_response(execute_statement_resp)


  @pytest.mark.execute_serially
  @CustomClusterTestSuite.with_args("--server_name=server1\
      --authorization_policy_file=%s\
      --authorized_proxy_user_config=hue=%s" % (AUTH_POLICY_FILE, getuser()))
  def test_access_runtime_profile(self):
    from tests.hs2.test_hs2 import TestHS2
    open_session_req = TCLIService.TOpenSessionReq()
    open_session_req.username = getuser()
    open_session_req.configuration = dict()
    resp = self.hs2_client.OpenSession(open_session_req)
    TestHS2.check_response(resp)

    # Current user can't access view's underlying tables
    self.session_handle = resp.sessionHandle
    execute_statement_req = TCLIService.TExecuteStatementReq()
    execute_statement_req.sessionHandle = self.session_handle
    execute_statement_req.statement = "explain select * from functional.complex_view"
    execute_statement_resp = self.hs2_client.ExecuteStatement(execute_statement_req)
    assert 'User \'%s\' does not have privileges to EXPLAIN' % getuser() in\
        str(execute_statement_resp)
    # User should not have access to the runtime profile
    self.__run_stmt_and_verify_profile_access("select * from functional.complex_view",
        False, False)
    self.__run_stmt_and_verify_profile_access("select * from functional.complex_view",
        False, True)

    # Repeat as a delegated user
    open_session_req.username = 'hue'
    open_session_req.configuration = dict()
    # Delegated user is the current user
    open_session_req.configuration['impala.doas.user'] = getuser()
    resp = self.hs2_client.OpenSession(open_session_req)
    TestHS2.check_response(resp)
    self.session_handle = resp.sessionHandle
    # User should not have access to the runtime profile
    self.__run_stmt_and_verify_profile_access("select * from functional.complex_view",
        False, False)
    self.__run_stmt_and_verify_profile_access("select * from functional.complex_view",
        False, True)

    # Create a view for which the user has access to the underlying tables.
    open_session_req.username = getuser()
    open_session_req.configuration = dict()
    resp = self.hs2_client.OpenSession(open_session_req)
    TestHS2.check_response(resp)
    self.session_handle = resp.sessionHandle
    execute_statement_req = TCLIService.TExecuteStatementReq()
    execute_statement_req.sessionHandle = self.session_handle
    execute_statement_req.statement = """create view if not exists tpch.customer_view as
        select * from tpch.customer limit 1"""
    execute_statement_resp = self.hs2_client.ExecuteStatement(execute_statement_req)
    TestHS2.check_response(execute_statement_resp)

    # User should be able to run EXPLAIN
    execute_statement_req = TCLIService.TExecuteStatementReq()
    execute_statement_req.sessionHandle = self.session_handle
    execute_statement_req.statement = """explain select * from tpch.customer_view"""
    execute_statement_resp = self.hs2_client.ExecuteStatement(execute_statement_req)
    TestHS2.check_response(execute_statement_resp)

    # User should have access to the runtime profile and exec summary
    self.__run_stmt_and_verify_profile_access("select * from tpch.customer_view", True,
        False)
    self.__run_stmt_and_verify_profile_access("select * from tpch.customer_view", True,
        True)

    # Repeat as a delegated user
    open_session_req.username = 'hue'
    open_session_req.configuration = dict()
    # Delegated user is the current user
    open_session_req.configuration['impala.doas.user'] = getuser()
    resp = self.hs2_client.OpenSession(open_session_req)
    TestHS2.check_response(resp)
    self.session_handle = resp.sessionHandle
    # User should have access to the runtime profile and exec summary
    self.__run_stmt_and_verify_profile_access("select * from tpch.customer_view",
        True, False)
    self.__run_stmt_and_verify_profile_access("select * from tpch.customer_view",
        True, True)

    # Clean up
    execute_statement_req = TCLIService.TExecuteStatementReq()
    execute_statement_req.sessionHandle = self.session_handle
    execute_statement_req.statement = "drop view if exists tpch.customer_view"
    execute_statement_resp = self.hs2_client.ExecuteStatement(execute_statement_req)
    TestHS2.check_response(execute_statement_resp)

  @pytest.mark.execute_serially
  @CustomClusterTestSuite.with_args("--server_name=server1\
      --authorization_policy_file=%s\
      --authorized_proxy_user_config=foo=bar;hue=%s\
      --abort_on_failed_audit_event=false\
      --audit_event_log_dir=%s" % (AUTH_POLICY_FILE, getuser(), AUDIT_LOG_DIR))
  def test_user_impersonation(self):
    """End-to-end user impersonation + authorization test"""
    self.__test_impersonation()

  @pytest.mark.execute_serially
  @CustomClusterTestSuite.with_args("--server_name=server1\
        --authorization_policy_file=%s\
        --authorized_proxy_user_config=hue=bar\
        --authorized_proxy_group_config=foo=bar;hue=%s\
        --abort_on_failed_audit_event=false\
        --audit_event_log_dir=%s" % (AUTH_POLICY_FILE,
                                     grp.getgrgid(os.getgid()).gr_name,
                                     AUDIT_LOG_DIR))
  def test_group_impersonation(self):
    """End-to-end group impersonation + authorization test"""
    self.__test_impersonation()

  @pytest.mark.execute_serially
  @CustomClusterTestSuite.with_args("--server_name=server1\
        --authorization_policy_file=%s\
        --authorized_proxy_user_config=foo=bar\
        --authorized_proxy_group_config=foo=bar\
        --abort_on_failed_audit_event=false\
        --audit_event_log_dir=%s" % (AUTH_POLICY_FILE, AUDIT_LOG_DIR))
  def test_no_matching_user_and_group_impersonation(self):
    open_session_req = TCLIService.TOpenSessionReq()
    open_session_req.username = 'hue'
    open_session_req.configuration = dict()
    open_session_req.configuration['impala.doas.user'] = 'abc'
    resp = self.hs2_client.OpenSession(open_session_req)
    assert 'User \'hue\' is not authorized to delegate to \'abc\'' in str(resp)

  def __test_impersonation(self):
    """End-to-end impersonation + authorization test. Expects authorization to be
    configured before running this test"""
    # TODO: To reuse the HS2 utility code from the TestHS2 test suite we need to import
    # the module within this test function, rather than as a top-level import. This way
    # the tests in that module will not get pulled when executing this test suite. The fix
    # is to split the utility code out of the TestHS2 class and support HS2 as a first
    # class citizen in our test framework.
    from tests.hs2.test_hs2 import TestHS2
    open_session_req = TCLIService.TOpenSessionReq()
    # Connected user is 'hue'
    open_session_req.username = 'hue'
    open_session_req.configuration = dict()
    # Delegated user is the current user
    open_session_req.configuration['impala.doas.user'] = getuser()
    resp = self.hs2_client.OpenSession(open_session_req)
    TestHS2.check_response(resp)

    # Try to query a table we are not authorized to access.
    self.session_handle = resp.sessionHandle
    execute_statement_req = TCLIService.TExecuteStatementReq()
    execute_statement_req.sessionHandle = self.session_handle
    execute_statement_req.statement = "describe tpch_seq.lineitem"
    execute_statement_resp = self.hs2_client.ExecuteStatement(execute_statement_req)
    assert 'User \'%s\' does not have privileges to access' % getuser() in\
        str(execute_statement_resp)

    assert self.__wait_for_audit_record(user=getuser(), impersonator='hue'),\
        'No matching audit event recorded in time window'

    # Now try the same operation on a table we are authorized to access.
    execute_statement_req = TCLIService.TExecuteStatementReq()
    execute_statement_req.sessionHandle = self.session_handle
    execute_statement_req.statement = "describe tpch.lineitem"
    execute_statement_resp = self.hs2_client.ExecuteStatement(execute_statement_req)

    TestHS2.check_response(execute_statement_resp)

    # Verify the correct user information is in the runtime profile
    query_id = operation_id_to_query_id(
        execute_statement_resp.operationHandle.operationId)
    profile_page = self.cluster.impalads[0].service.read_query_profile_page(query_id)
    self.__verify_profile_user_fields(profile_page, effective_user=getuser(),
        delegated_user=getuser(), connected_user='hue')

    # Try to user we are not authorized to delegate to.
    open_session_req.configuration['impala.doas.user'] = 'some_user'
    resp = self.hs2_client.OpenSession(open_session_req)
    assert 'User \'hue\' is not authorized to delegate to \'some_user\'' in str(resp)

    # Create a new session which does not have a do_as_user.
    open_session_req.username = 'hue'
    open_session_req.configuration = dict()
    resp = self.hs2_client.OpenSession(open_session_req)
    TestHS2.check_response(resp)

    # Run a simple query, which should succeed.
    execute_statement_req = TCLIService.TExecuteStatementReq()
    execute_statement_req.sessionHandle = resp.sessionHandle
    execute_statement_req.statement = "select 1"
    execute_statement_resp = self.hs2_client.ExecuteStatement(execute_statement_req)
    TestHS2.check_response(execute_statement_resp)

    # Verify the correct user information is in the runtime profile. Since there is
    # no do_as_user the Delegated User field should be empty.
    query_id = operation_id_to_query_id(
        execute_statement_resp.operationHandle.operationId)

    profile_page = self.cluster.impalads[0].service.read_query_profile_page(query_id)
    self.__verify_profile_user_fields(profile_page, effective_user='hue',
        delegated_user='', connected_user='hue')

    self.socket.close()
    self.socket = None

  def __verify_profile_user_fields(self, profile_str, effective_user, connected_user,
      delegated_user):
    """Verifies the given runtime profile string contains the specified values for
    User, Connected User, and Delegated User"""
    assert '\n    User: %s\n' % effective_user in profile_str
    assert '\n    Connected User: %s\n' % connected_user in profile_str
    assert '\n    Delegated User: %s\n' % delegated_user in profile_str

  def __wait_for_audit_record(self, user, impersonator, timeout_secs=30):
    """Waits until an audit log record is found that contains the given user and
    impersonator, or until the timeout is reached.
    """
    # The audit event might not show up immediately (the audit logs are flushed to disk
    # on regular intervals), so poll the audit event logs until a matching record is
    # found.
    start_time = time()
    while time() - start_time < timeout_secs:
      for audit_file_name in os.listdir(self.AUDIT_LOG_DIR):
        if self.__find_matching_audit_record(audit_file_name, user, impersonator):
          return True
      sleep(1)
    return False

  def __find_matching_audit_record(self, audit_file_name, user, impersonator):
    with open(os.path.join(self.AUDIT_LOG_DIR, audit_file_name)) as audit_log_file:
      for line in audit_log_file.readlines():
          json_dict = json.loads(line)
          if len(json_dict) == 0: continue
          if json_dict[min(json_dict)]['user'] == user and\
              json_dict[min(json_dict)]['impersonator'] == impersonator:
            return True
    return False

  def __run_stmt_and_verify_profile_access(self, stmt, has_access, close_operation):
    """Runs 'stmt' and retrieves the runtime profile and exec summary. If
      'has_access' is true, it verifies that no runtime profile or exec summary are
      returned. If 'close_operation' is true, make sure the operation is closed before
      retrieving the profile and exec summary."""
    from tests.hs2.test_hs2 import TestHS2
    execute_statement_req = TCLIService.TExecuteStatementReq()
    execute_statement_req.sessionHandle = self.session_handle
    execute_statement_req.statement = stmt
    execute_statement_resp = self.hs2_client.ExecuteStatement(execute_statement_req)
    TestHS2.check_response(execute_statement_resp)

    if close_operation:
      close_operation_req = TCLIService.TCloseOperationReq()
      close_operation_req.operationHandle = execute_statement_resp.operationHandle
      TestHS2.check_response(self.hs2_client.CloseOperation(close_operation_req))

    get_profile_req = ImpalaHiveServer2Service.TGetRuntimeProfileReq()
    get_profile_req.operationHandle = execute_statement_resp.operationHandle
    get_profile_req.sessionHandle = self.session_handle
    get_profile_resp = self.hs2_client.GetRuntimeProfile(get_profile_req)

    if has_access:
      TestHS2.check_response(get_profile_resp)
      assert "Plan: " in get_profile_resp.profile
    else:
      assert "User %s is not authorized to access the runtime profile or "\
          "execution summary." % (getuser()) in str(get_profile_resp)

    exec_summary_req = ImpalaHiveServer2Service.TGetExecSummaryReq()
    exec_summary_req.operationHandle = execute_statement_resp.operationHandle
    exec_summary_req.sessionHandle = self.session_handle
    exec_summary_resp = self.hs2_client.GetExecSummary(exec_summary_req)

    if has_access:
      TestHS2.check_response(exec_summary_resp)
    else:
      assert "User %s is not authorized to access the runtime profile or "\
          "execution summary." % (getuser()) in str(exec_summary_resp)

  @pytest.mark.execute_serially
  @CustomClusterTestSuite.with_args(
      impalad_args="--server_name=server1 --sentry_config=" + SENTRY_CONFIG_FILE,
      catalogd_args="--sentry_config=" + SENTRY_CONFIG_FILE,
      impala_log_dir=tempfile.mkdtemp(prefix="test_deprecated_none_",
      dir=os.getenv("LOG_DIR")))
  def test_deprecated_flag_doesnt_show(self):
    assert_no_files_in_dir_contain(self.impala_log_dir, "authorization_policy_file " +
        "flag is deprecated. Object Ownership feature is not supported")

  @pytest.mark.execute_serially
  @CustomClusterTestSuite.with_args("--server_name=server1\
      --authorization_policy_file=%s\
      --authorization_policy_provider_class=%s" % (AUTH_POLICY_FILE,
       "org.apache.sentry.provider.file.LocalGroupResourceAuthorizationProvider"),
      impala_log_dir=tempfile.mkdtemp(prefix="test_deprecated_",
      dir=os.getenv("LOG_DIR")))
  def test_deprecated_flags(self):
    assert_file_in_dir_contains(self.impala_log_dir, "authorization_policy_file flag" +
        " is deprecated. Object Ownership feature is not supported")

  @pytest.mark.execute_serially
  @CustomClusterTestSuite.with_args(
    impalad_args="--server_name=server1 --sentry_config=%s" % SENTRY_CONFIG_FILE,
    catalogd_args="--sentry_config=%s" % SENTRY_CONFIG_FILE,
    impala_log_dir=tempfile.mkdtemp(prefix="test_catalog_restart_",
                                    dir=os.getenv("LOG_DIR")))
  def test_catalog_restart(self, unique_role):
    """IMPALA-7713: Tests that a catalogd restart when authorization is enabled should
    reset the previous privileges stored in impalad's catalog to avoid stale privilege
    data in the impalad's catalog."""
    def assert_privileges():
      result = self.client.execute("show grant role %s_foo" % unique_role)
      TestAuthorization._check_privileges(result, [["database", "functional",
                                                    "", "", "", "all", "false"]])

      result = self.client.execute("show grant role %s_bar" % unique_role)
      TestAuthorization._check_privileges(result, [["database", "functional_kudu",
                                                    "", "", "", "all", "false"]])

      result = self.client.execute("show grant role %s_baz" % unique_role)
      TestAuthorization._check_privileges(result, [["database", "functional_avro",
                                                    "", "", "", "all", "false"]])

    self.role_cleanup(unique_role)
    try:
      self.client.execute("create role %s_foo" % unique_role)
      self.client.execute("create role %s_bar" % unique_role)
      self.client.execute("create role %s_baz" % unique_role)
      self.client.execute("grant all on database functional to role %s_foo" %
                          unique_role)
      self.client.execute("grant all on database functional_kudu to role %s_bar" %
                          unique_role)
      self.client.execute("grant all on database functional_avro to role %s_baz" %
                          unique_role)

      assert_privileges()
      self._start_impala_cluster(["--catalogd_args=--sentry_config=%s" %
                                  SENTRY_CONFIG_FILE, "--restart_catalogd_only"])
      assert_privileges()
    finally:
      self.role_cleanup(unique_role)

  def role_cleanup(self, role_name_match):
    """Cleans up any roles that match the given role name."""
    for role_name in self.client.execute("show roles").data:
      if role_name_match in role_name:
        self.client.execute("drop role %s" % role_name)

  @staticmethod
  def _check_privileges(result, expected):
    def columns(row):
      cols = row.split("\t")
      return cols[0:len(cols) - 1]
    assert map(columns, result.data) == expected

  @pytest.mark.execute_serially
  @CustomClusterTestSuite.with_args(
    impalad_args="--server_name=server1 --sentry_config=%s" % SENTRY_CONFIG_FILE,
    catalogd_args="--sentry_config=%s" % SENTRY_CONFIG_FILE,
    impala_log_dir=tempfile.mkdtemp(prefix="test_catalog_restart_",
                                    dir=os.getenv("LOG_DIR")))
  def test_catalog_object(self, unique_role):
    """IMPALA-7721: Tests /catalog_object web API for principal and privilege"""
    self.role_cleanup(unique_role)
    try:
      self.client.execute("create role %s" % unique_role)
      self.client.execute("grant select on database functional to role %s" % unique_role)
      for service in [self.cluster.catalogd.service,
                      self.cluster.get_first_impalad().service]:
        obj_dump = service.get_catalog_object_dump("PRINCIPAL", "%s.ROLE" % unique_role)
        assert "catalog_version" in obj_dump

        # Get the privilege associated with that principal ID.
        principal_id = re.search(r"principal_id \(i32\) = (\d+)", obj_dump)
        assert principal_id is not None
        obj_dump = service.get_catalog_object_dump("PRIVILEGE", urllib.quote(
            "server=server1->db=functional->action=select->grantoption=false.%s.ROLE" %
            principal_id.group(1)))
        assert "catalog_version" in obj_dump

        # Get the principal that does not exist.
        obj_dump = service.get_catalog_object_dump("PRINCIPAL", "doesnotexist.ROLE")
        assert "CatalogException" in obj_dump

        # Get the privilege that does not exist.
        obj_dump = service.get_catalog_object_dump("PRIVILEGE", urllib.quote(
            "server=server1->db=doesntexist->action=select->grantoption=false.%s.ROLE" %
            principal_id.group(1)))
        assert "CatalogException" in obj_dump
    finally:
      self.role_cleanup(unique_role)

  @pytest.mark.execute_serially
  @CustomClusterTestSuite.with_args(
    impalad_args="--server_name=server1 --sentry_config=%s" % SENTRY_CONFIG_FILE,
    catalogd_args="--sentry_config=%s --sentry_catalog_polling_frequency_s=3600" %
                  SENTRY_CONFIG_FILE,
    impala_log_dir=tempfile.mkdtemp(prefix="test_invalidate_metadata_sentry_unavailable_",
                                    dir=os.getenv("LOG_DIR")))
  def test_invalidate_metadata_sentry_unavailable(self, unique_role):
    """IMPALA-7824: Tests that running INVALIDATE METADATA when Sentry is unavailable
    should not cause Impala to hang."""
    self.role_cleanup(unique_role)
    try:
      group_name = grp.getgrnam(getuser()).gr_name
      self.client.execute("create role %s" % unique_role)
      self.client.execute("grant all on server to role %s" % unique_role)
      self.client.execute("grant role %s to group `%s`" % (unique_role, group_name))

      self._stop_sentry_service()
      # Calling INVALIDATE METADATA when Sentry is unavailable should return an error.
      result = self.execute_query_expect_failure(self.client, "invalidate metadata")
      result_str = str(result)
      assert "MESSAGE: CatalogException: Error refreshing authorization policy:" \
             in result_str
      assert "CAUSED BY: ImpalaRuntimeException: Error refreshing authorization policy." \
             " Sentry is unavailable. Ensure Sentry is up:" in result_str

      self._start_sentry_service(SENTRY_CONFIG_FILE)
      # Calling INVALIDATE METADATA after Sentry is up should not return an error.
      self.execute_query_expect_success(self.client, "invalidate metadata")
    finally:
      self.role_cleanup(unique_role)

  @pytest.mark.execute_serially
  @CustomClusterTestSuite.with_args(
      impalad_args="--server_name=server1 --sentry_config=%s" % SENTRY_CONFIG_FILE,
      catalogd_args="--sentry_config=%s --sentry_catalog_polling_frequency_s=3600 " %
                    SENTRY_CONFIG_FILE,
      impala_log_dir=tempfile.mkdtemp(prefix="test_refresh_authorization_",
                                      dir=os.getenv("LOG_DIR")))
  def test_refresh_authorization(self, unique_role):
    """Tests refresh authorization statement by adding and removing roles and privileges
       externally. The long Sentry polling is used so that any authorization metadata
       updated externally does not get polled by Impala in order to test an an explicit
       call to refresh authorization statement."""
    group_name = grp.getgrnam(getuser()).gr_name
    self.role_cleanup(unique_role)
    for sync_ddl in [1, 0]:
      query_options = {'sync_ddl': sync_ddl}
      clients = []
      if sync_ddl:
        # When sync_ddl is True, we want to ensure the changes are propagated to all
        # coordinators.
        for impalad in self.cluster.impalads:
          clients.append(impalad.service.create_beeswax_client())
      else:
        clients.append(self.client)
      try:
        self.client.execute("create role %s" % unique_role)
        self.client.execute("grant role %s to group `%s`" % (unique_role, group_name))
        self.client.execute("grant refresh on server to %s" % unique_role)

        self.validate_refresh_authorization_roles(unique_role, query_options, clients)
        self.validate_refresh_authorization_privileges(unique_role, query_options,
                                                       clients)
      finally:
        self.role_cleanup(unique_role)

  def validate_refresh_authorization_roles(self, unique_role, query_options, clients):
    """This method tests refresh authorization statement by adding and removing
       roles externally."""
    try:
      # Create two roles inside Impala.
      self.client.execute("create role %s_internal1" % unique_role)
      self.client.execute("create role %s_internal2" % unique_role)
      # Drop an existing role (_internal1) outside Impala.
      role = "%s_internal1" % unique_role
      subprocess.check_call(
        ["/bin/bash", "-c",
         "%s/bin/sentryShell --conf %s/sentry-site.xml -dr -r %s" %
         (os.getenv("SENTRY_HOME"), os.getenv("SENTRY_CONF_DIR"), role)],
        stdout=sys.stdout, stderr=sys.stderr)

      result = self.execute_query_expect_success(self.client, "show roles")
      assert any(role in x for x in result.data)
      self.execute_query_expect_success(self.client, "refresh authorization",
                                        query_options=query_options)
      for client in clients:
        result = self.execute_query_expect_success(client, "show roles")
        assert not any(role in x for x in result.data)

      # Add a new role outside Impala.
      role = "%s_external" % unique_role
      subprocess.check_call(
          ["/bin/bash", "-c",
           "%s/bin/sentryShell --conf %s/sentry-site.xml -cr -r %s" %
           (os.getenv("SENTRY_HOME"), os.getenv("SENTRY_CONF_DIR"), role)],
          stdout=sys.stdout, stderr=sys.stderr)

      result = self.execute_query_expect_success(self.client, "show roles")
      assert not any(role in x for x in result.data)
      self.execute_query_expect_success(self.client, "refresh authorization",
                                        query_options=query_options)
      for client in clients:
        result = self.execute_query_expect_success(client, "show roles")
        assert any(role in x for x in result.data)
    finally:
      for suffix in ["internal1", "internal2", "external"]:
        self.role_cleanup("%s_%s" % (unique_role, suffix))

  def validate_refresh_authorization_privileges(self, unique_role, query_options,
                                                clients):
    """This method tests refresh authorization statement by adding and removing
       privileges externally."""
    # Grant select privilege outside Impala.
    subprocess.check_call(
        ["/bin/bash", "-c",
         "%s/bin/sentryShell --conf %s/sentry-site.xml -gpr -p "
         "'server=server1->db=functional->table=alltypes->action=select' -r %s" %
         (os.getenv("SENTRY_HOME"), os.getenv("SENTRY_CONF_DIR"), unique_role)],
        stdout=sys.stdout, stderr=sys.stderr)

    # Before refresh authorization, there should only be one refresh privilege.
    result = self.execute_query_expect_success(self.client, "show grant role %s" %
                                               unique_role)
    assert len(result.data) == 1
    assert any("refresh" in x for x in result.data)

    for client in clients:
      self.execute_query_expect_failure(client,
                                        "select * from functional.alltypes limit 1")

    self.execute_query_expect_success(self.client, "refresh authorization",
                                      query_options=query_options)

    for client in clients:
      # Ensure select privilege was granted after refresh authorization.
      result = self.execute_query_expect_success(client, "show grant role %s" %
                                                 unique_role)
      assert len(result.data) == 2
      assert any("select" in x for x in result.data)
      assert any("refresh" in x for x in result.data)
      self.execute_query_expect_success(client,
                                        "select * from functional.alltypes limit 1")

  @pytest.mark.execute_serially
  @CustomClusterTestSuite.with_args(
    impalad_args="--server_name=server1 --sentry_config=%s "
                 "--authorized_proxy_user_config=%s=* "
                 "--simplify_check_on_show_tables=true" %
                 (SENTRY_CONFIG_FILE, getuser()),
    catalogd_args="--sentry_config={0}".format(SENTRY_CONFIG_FILE),
    sentry_config=SENTRY_CONFIG_FILE_OO,  # Enable Sentry Object Ownership
    sentry_log_dir="{0}/test_fast_show_tables_with_sentry".format(SENTRY_BASE_LOG_DIR))
  def test_fast_show_tables_with_sentry(self, unique_role, unique_name):
    unique_db = unique_name + "_db"
    # TODO: can we create and use a temp username instead of using root?
    another_user = 'root'
    another_user_grp = 'root'
    self.role_cleanup(unique_role)
    try:
      self.client.execute("create role %s" % unique_role)
      self.client.execute("grant create on server to role %s" % unique_role)
      self.client.execute("grant drop on server to role %s" % unique_role)
      self.client.execute("grant role %s to group %s" %
                          (unique_role, grp.getgrnam(getuser()).gr_name))

      self.client.execute("drop database if exists %s cascade" % unique_db)
      self.client.execute("create database %s" % unique_db)
      for priv in PRIVILEGES:
        self.client.execute("create table %s.tbl_%s (i int)" % (unique_db, priv))
        self.client.execute("grant {0} on table {1}.tbl_{2} to role {3}"
                            .format(priv, unique_db, priv, unique_role))
      self.client.execute("grant role %s to group %s" %
                          (unique_role, another_user_grp))

      # Owner (current user) can still see all the tables
      result = self.client.execute("show tables in %s" % unique_db)
      assert result.data == ["tbl_%s" % p for p in PRIVILEGES]

      # Check SHOW TABLES using another username
      # Create another client so we can user another username
      root_impalad_client = self.create_impala_client()
      result = self.execute_query_expect_success(
        root_impalad_client, "show tables in %s" % unique_db, user=another_user)
      # Only show tables with privileges implying SELECT privilege
      assert result.data == ['tbl_all', 'tbl_select']
    finally:
      self.role_cleanup(unique_role)
      self.client.execute("drop database if exists %s cascade" % unique_db)

  @pytest.mark.execute_serially
  @CustomClusterTestSuite.with_args(
    impalad_args="--server-name=server1 --ranger_service_type=hive "
                 "--ranger_app_id=impala --authorization_provider=ranger "
                 "--simplify_check_on_show_tables=true",
    catalogd_args="--server-name=server1 --ranger_service_type=hive "
                  "--ranger_app_id=impala --authorization_provider=ranger")
  def test_fast_show_tables_with_ranger(self, unique_role, unique_name):
    unique_db = unique_name + "_db"
    admin_client = self.create_impala_client()
    try:
      admin_client.execute("drop database if exists %s cascade" % unique_db, user=ADMIN)
      admin_client.execute("create database %s" % unique_db, user=ADMIN)
      for priv in PRIVILEGES:
        admin_client.execute("create table %s.tbl_%s (i int)" % (unique_db, priv))
        admin_client.execute("grant {0} on table {1}.tbl_{2} to user {3}"
                            .format(priv, unique_db, priv, getuser()))

      # Admin can still see all the tables
      result = admin_client.execute("show tables in %s" % unique_db)
      assert result.data == ["tbl_%s" % p for p in PRIVILEGES]

      # Check SHOW TABLES using another username
      result = self.client.execute("show tables in %s" % unique_db)
      # Only show tables with privileges implying SELECT privilege
      assert result.data == ['tbl_all', 'tbl_select']
    finally:
      admin_client.execute("drop database if exists %s cascade" % unique_db)
