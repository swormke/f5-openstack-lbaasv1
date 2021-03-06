#!/bin/bash

echo "Building RedHat packages..."
buildroot="/tmp/bdir"

cp -R /var/bdir /tmp
make -C ${buildroot} rpms

mkdir -p /var/bdir/build/el6
for d in agent common driver;
do
	for p in `ls ${buildroot}/$d/dist/*.rpm`;
	do
		mv $p ${p%%.rpm}.el6.rpm
	done
	cp -R ${buildroot}/$d/dist/* /var/bdir/build/el6
done

