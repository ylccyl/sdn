import subprocess

container_string = 'c'
container_num = 12
for i in range(1, container_num + 1):
    if i < 10:
        container_name = container_string + '0' + str(i)
    else:
        container_name = container_string + str(i)

    command = "sudo docker run --privileged -i -d -t --name=%s   " \
              "-v /home/z/ryu/ryu/app:/home/ryu/ryu/app ryu:5 /bin/bash" % container_name

    subprocess.run(command, shell=True)
    print(command)
print("-----------------------------容器创建完毕 -------------------------")
print('\n')
print('\n')
print('\n')
print('\n')

veth_peer = []
link_string = 'veth'
link_array = [(1, 2), (1, 3), (2, 5), (2, 4),
              (3, 5), (3, 6), (4, 5), (4, 11),
              (4, 12),(5, 6), (5, 10), (6, 10),
              (6, 11),(7, 8), (7, 9), (8, 10), (8,9),
              (9, 11), (9 ,12),(10, 11), (11, 12)]
for temp in link_array:
    src, dst = temp

    if int(src) < 10:
        src = '0' + str(src)

    if int(dst) < 10:
        dst = '0' + str(dst)

    peer_1 = link_string + str(src) + str(dst)
    veth_peer.append(peer_1)
    peer_2 = link_string + str(dst) + str(src)
    veth_peer.append(peer_2)
    command = "sudo ip link add %s type veth peer name %s" % (peer_1, peer_2)
    # command = "sudo ip link del %s type veth peer name %s" % (peer_1, peer_2)

    subprocess.run(command, shell=True)
    print(command)
print("-----------------------------veth peer对创建完毕 -------------------------")
print('\n')
print('\n')
print('\n')

for i in range(1,container_num + 1):
    if i < 10:
        container_name = container_string + '0' + str(i)
    else:
        container_name = container_string + str(i)

    for temp in veth_peer:
        if temp[4:6] == container_name[1:]:
            print(temp, container_name)
            command = "sudo ip link set %s netns $(sudo docker inspect -f '{{.State.Pid}}' %s)" % (temp, container_name)
            command_1 = "sudo docker exec %s ip link set %s up" % (container_name, temp)
            print(command)
            print(command_1)
            subprocess.run(command, shell=True)
            subprocess.run(command_1, shell=True)

print("-----------------------------将创建的端口移入到对应的命名空间并开启网卡完毕-------------------------------")
print('\n')
print('\n')
print('\n')
#
#
switch_string = 's'
id_1 = '000000000000000'
id_2 = '00000000000000'
ip = '172.17.0.'
ip_suffix = '/16'
for i in range(1, container_num+1):
    if i < 10:
        container_name = container_string + '0' + str(i)
        switch_dpid = id_1 + str(i)
    else:
        container_name = container_string + str(i)
        switch_dpid = id_2 + str(i)

    switch_ovs = switch_string + str(i)
    switch_ip_address = ip + str(i + 1) + ip_suffix

    command = "sudo docker exec %s service openvswitch-switch start" % container_name
    command_1 = "sudo docker exec %s ovs-vsctl add-br %s" % (container_name, switch_ovs)
    command_2 = "sudo docker exec %s ovs-vsctl set bridge %s other_config:datapath-id=%s" \
                % (container_name, switch_ovs, switch_dpid)
    command_3 = "sudo docker exec %s ovs-vsctl set-fail-mode %s secure" % (container_name, switch_ovs)
    subprocess.run(command, shell=True)
    subprocess.run(command_1, shell=True)
    subprocess.run(command_2, shell=True)
    subprocess.run(command_3, shell=True)
    print(command)
    print(command_1)
    print(command_2)
    print(command_3)

    for temp in veth_peer:
        if temp[4:6] == container_name[1:]:
            command_4 = "sudo docker exec %s ovs-vsctl add-port %s %s" % (container_name, switch_ovs, temp)
            subprocess.run(command_4, shell=True)
            print(command_4, temp, container_name)

    if i == 1 or i == 7:
        command_5 = "sudo docker exec %s ovs-vsctl add-port %s eth0" % (container_name, switch_ovs)
        command_6 = "sudo docker exec %s ip addr flush dev eth0" % container_name
        subprocess.run(command_5, shell=True)
        subprocess.run(command_6, shell=True)
        print(command_5)
        print(command_6)
    else:
        command_7 = "sudo docker exec %s ip link delete eth0" % container_name
        subprocess.run(command_7, shell=True)
        print(command_7)

    command_8 = "sudo docker exec %s ip addr add %s dev %s" % (container_name, switch_ip_address, switch_ovs)
    command_9 = "sudo docker exec %s ip link set %s up" % (container_name, switch_ovs)
    command_10 = "sudo docker exec %s ip route add default dev %s" % (container_name, switch_ovs)

    subprocess.run(command_8, shell=True)
    subprocess.run(command_9, shell=True)
    subprocess.run(command_10, shell=True)
    print(command_8)
    print(command_9)
    print(command_10)

    print("-----------------------------为容器%s配置完毕 -------------------------" % container_name)
