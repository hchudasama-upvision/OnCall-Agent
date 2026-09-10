AWS SLED (us-gov-1) - Track Job
[rubrikcluster01] [ Failed: LDAP service is unhealthy. ]
uk-1 : uk-prod - ALBUnhealthyHostCritical (*Summary:* Application Load Balancer app/uk-prod-fastcore-app-http/992c86a8ba9f5fb3 has at least 1 unhealthy instances for at least 15m)
us-1 : ASG Has No In-Service Instances


PandoLogic - HostCPUUtilizationCritical (RealMatch-Cluster01 RealMatch-Datacenter ny1esx9679.verimatch.com ny1wv5840.verimatch.com vmware_vcenter critical n/a)
uk-prod - ElastiCacheConnectionsCritical (uk-prod-r32 0001 aws_elasticache 10.66.10.17:10003 cloudwatch_exporter critical)

Exclude Alerts ( directly need Human support)
ops-prom : EndpointDown
  - should put message with which endpoint is down in comms-noc, but have to perform action manually by human, because find out, if some ITSM is going on, or some changes has been done in past ( could search for past changes in particular channels)

ops-prom : TLSCertificateExpiryUnder28d
  - have to create NOC ticket to Renew that Ex. https://veritone.atlassian.net/browse/NOC-12626?issueKey=NOC-12626


Threat Alert: www.thejobnetwork.com - realmatch
  - have to login in victorOps and paste whole block of text in comms-noc alert's thread

KubePodsNotReady -- DONE
RDS_CPUUtilizationAvgCriticalCore -- DONE
Engine backlog critical for 30m ( same as 15% engine failure rate one) -- DONE
Rekognition-ThrottledCount-High-wpsc01 -- DONE
ops-prom : EndpointDown -- DONE
High concurrent_requests for core-admin-server ( same as High nodejs_active_handles for core-admin-server) -- DONE


IIS: Application pool FeedParser is not in Running state -- SHOULD DONE BY HUMAN ONLY (TOO MUCH CONSTRAINTS)
