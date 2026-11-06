import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import os
import sys

class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env={'INSTANCE_IDS':'i-0123456789abcdef0','GITHUB_REPOSITORY':'owner/repo','GITHUB_APP_ID':'1','GITHUB_INSTALLATION_ID':'2'}
        spec=importlib.util.spec_from_file_location('agent_test',Path(__file__).parents[1]/'agent/agent.py')
        self.agent=importlib.util.module_from_spec(spec)
        with patch.dict(os.environ,env), patch.dict(sys.modules,{'jwt':Mock(),'requests':Mock()}), patch('boto3.client'):
            spec.loader.exec_module(self.agent)
        self.agent.STATE=Path(self.tmp.name)/'incidents.json'
        self.agent.session=Mock()
        self.state={}
    def tearDown(self): self.tmp.cleanup()
    def status(self,state):
        self.agent.EC2.describe_instances.return_value={'Reservations':[{'Instances':[{'InstanceId':'i-0123456789abcdef0','State':{'Name':state}}]}]}
    def test_dispatch_once_per_stopped_incident(self):
        self.status('stopped')
        self.agent.monitor_once(self.state)
        self.agent.monitor_once(self.state)
        self.assertEqual(self.agent.session.return_value.post.call_count,1)
        self.assertEqual(self.state['i-0123456789abcdef0']['dispatch'],'accepted')
    def test_uncertain_dispatch_does_not_retry(self):
        self.status('stopped')
        self.agent.session.return_value.post.side_effect=TimeoutError('uncertain timeout')
        with self.assertLogs(level='ERROR'):
            self.agent.monitor_once(self.state)
        self.agent.monitor_once(self.state)
        self.assertEqual(self.agent.session.return_value.post.call_count,1)
    def test_stopping_does_not_dispatch(self):
        self.status('stopping')
        self.agent.monitor_once(self.state)
        self.agent.session.assert_not_called()
    def test_running_rearms_only_after_five_minutes(self):
        iid='i-0123456789abcdef0'
        self.state[iid]={'dispatch':'accepted'}
        self.status('running')
        with patch.object(self.agent.time,'time',return_value=1000): self.agent.monitor_once(self.state)
        self.assertIn(iid,self.state)
        with patch.object(self.agent.time,'time',return_value=1301): self.agent.monitor_once(self.state)
        self.assertNotIn(iid,self.state)
if __name__=='__main__': unittest.main()
