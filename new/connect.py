import random
import subprocess
import time

c_s_1 = [1, 2, 3, 4, 5, 6]
c_s_2 = [8, 9, 10, 11, 12, 13]
container_string = 'c'
switch_string = 's'
container_num = 13
for i in range(container_num + 1):
    if i == 0 or i == 7:
        continue
    if i < 10:
        container_name = container_string + '0' + str(i)
    else:
        container_name = container_string + str(i)

    switch_ovs = switch_string + str(i)
    if i in c_s_1:
        command = "sudo docker exec %s ovs-vsctl set-controller %s tcp:172.17.0.2:6633 " % (container_name, switch_ovs)
        subprocess.run(command, shell=True)
        print(command)

    if i in c_s_2:
        command = "sudo docker exec %s ovs-vsctl set-controller %s tcp:172.17.0.9:6633 " % (container_name, switch_ovs)
        subprocess.run(command, shell=True)
        print(command)

    time.sleep(10)

print('\n')
print('\n')
print('\n')
print('\n')

time.sleep(20)
round = 1
while round < 1000:
    random_num = random.randint(0, container_num)
    if random_num in [0, 1, 7, 8]:
        round += 1
        time.sleep(5)
        continue

    if random_num < 10:
        container_name = container_string + '0' + str(random_num)
    else:
        container_name = container_string + str(random_num)

    switch_ovs = switch_string + str(random_num)

    command = "sudo docker exec %s ovs-vsctl del-controller %s" % (container_name, switch_ovs)
    print(command)
    subprocess.run(command, shell=True)
    round += 1
    time.sleep(60)

    if random_num in c_s_1:
        command = "sudo docker exec %s ovs-vsctl set-controller %s tcp:172.17.0.2:6633 " % (container_name, switch_ovs)
        # subprocess.run(command, shell=True)
        # print(command)

    if random_num in c_s_2:
        command = "sudo docker exec %s ovs-vsctl set-controller %s tcp:172.17.0.13:6633 " % (container_name, switch_ovs)

    print(command)
    subprocess.run(command, shell=True)
    time.sleep(10)
