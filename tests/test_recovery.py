import importlib.util
import unittest
from unittest.mock import Mock
from pathlib import Path
spec=importlib.util.spec_from_file_location("recovery",Path(__file__).parents[1]/"scripts/recovery.py")
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
class ValidationTests(unittest.TestCase):
    def test_unapproved_id_never_calls_aws(self):
        ec2=Mock()
        with self.assertRaises(ValueError):
            m.validate_instance(ec2,"i-0123456789abcdef0",["i-11111111111111111"])
        ec2.describe_instances.assert_not_called()
    def test_missing_tags_rejected(self):
        ec2=Mock()
        ec2.describe_instances.return_value={"Reservations":[{"Instances":[{"Tags":[]}]}]}
        with self.assertRaises(ValueError):
            m.validate_instance(ec2,"i-0123456789abcdef0",["i-0123456789abcdef0"])
    def test_valid_lab_instance(self):
        ec2=Mock()
        ec2.describe_instances.return_value={"Reservations":[{"Instances":[{"Tags":[{"Key":"Project","Value":"vj-recovery"},{"Key":"AutoRecover","Value":"true"},{"Key":"Name","Value":"web-1"}]}]}]}
        _,name=m.validate_instance(ec2,"i-0123456789abcdef0",["i-0123456789abcdef0"])
        self.assertEqual(name,"web-1")
if __name__=="__main__": unittest.main()
