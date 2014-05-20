# NOTE:
# 
# You need to install these packages on Ubunutu 12.04 to make this work:
# 
#     sudo apt-get install make python-stdeb fakeroot python-all rpm
# 
# 

PROJECT_DIR := $(shell dirname `pwd`)

VERSION := $(shell cat $(PROJECT_DIR)/VERSION|tr -d '\n'; echo -n '.'; cat $(PROJECT_DIR)/OS_RELEASE|tr -d '\n'; echo -n '-1')

PPA_DIST=precise
GPG_KEY=E1E513EF

default: deb_source

deb_source: build/f5-lbaas-driver_$(VERSION)_source.deb \
        build/f5-bigip-lbaas-agent_$(VERSION)_source.deb


build/f5-lbaas-driver_$(VERSION)_source.deb:
	(cd $(PROJECT_DIR)/driver; \
	rm -rf deb_dist; \
	sed -i.orig "s/\(.*version=\).*/\1\'$(VERSION)\',/g" setup.py; \
        python setup.py --command-packages=stdeb.command sdist_dsc --copyright-file $(PROJECT_DIR)/COPYRIGHT --suite $(PPA_DIST); \
        cd deb_dist/f5-lbaas-driver-$(VERSION); \
        dpkg-buildpackage -rfakeroot -S -k$(GPG_KEY); \
        cd $(PROJECT_DIR)/driver/deb_dist; \
        dput ppa:f5openstackcommunity/neutron f5-lbaas-driver_$(VERSION)-1_source.changes; \
        )

build/f5-bigip-lbaas-agent_$(VERSION)_source.deb:
	(cd $(PROJECT_DIR)/agent; \
	rm -rf deb_dist; \
	sed -i.orig "s/\(.*version=\).*/\1\'$(VERSION)\',/g" setup.py; \
        python setup.py --command-packages=stdeb.command sdist_dsc --copyright-file $(PROJECT_DIR)/COPYRIGHT --suite $(PPA_DIST); \
        cd deb_dist/f5-bigip-lbaas-agent-$(VERSION); \
        dpkg-buildpackage -rfakeroot -S -k$(GPG_KEY); \
        cd $(PROJECT_DIR)/agent/deb_dist; \
        dput ppa:f5openstackcommunity/neutron f5-bigip-lbaas-agent_$(VERSION)-1_source.changes; \
        )

clean: 
	rm -f $(PROJECT_DIR)/build/f5-bigip-lbaas-agent_*.deb
	(cd $(PROJECT_DIR)agent; \
	rm -rf deb_dist; \
        )
	rm -f $(PROJECT_DIR)/build/f5-lbaas-driver_*.deb
	(cd $(PROJECT_DIR)/driver; \
	rm -rf deb_dist; \
        )

