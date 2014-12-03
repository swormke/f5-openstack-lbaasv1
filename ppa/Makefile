# NOTE:
# 
# You need to install these packages on Ubunutu 12.04 to make this work:
# 
#     sudo apt-get install make python-stdeb fakeroot python-all rpm
# 
# 

PROJECT_DIR := $(shell dirname `pwd`)
VERSION := $(shell cat $(PROJECT_DIR)/VERSION|tr -d '\n';)
RELEASE := $(shell cat $(PROJECT DIR)/RELEASE|tr -d '\n';)

PPA_DIST=precise
GPG_KEY=0B8F094B

default: deb_source

deb_source: build/f5-lbaas-driver_$(VERSION)_source.deb \
        build/f5-bigip-lbaas-agent_$(VERSION)_source.deb


build/f5-lbaas-driver_$(VERSION)_source.deb:
	(cd $(PROJECT_DIR)/driver; \
	rm -rf deb_dist; \
	export PROJECT_DIR=$(PROJECT_DIR); \
	export VERSION=$(VERSION); \
	export RELEASE=$(RELEASE); \
        python setup.py --command-packages=stdeb.command sdist_dsc --copyright-file $(PROJECT_DIR)/COPYRIGHT --suite $(PPA_DIST); \
        cd deb_dist/f5-lbaas-driver-$(VERSION); \
        dpkg-buildpackage -rfakeroot -S -k$(GPG_KEY); \
        cd $(PROJECT_DIR)/driver/deb_dist; \
        dput ppa:f5openstackcommunity/havana f5-lbaas-driver_$(VERSION)-1_source.changes; \
        )

build/f5-bigip-lbaas-agent_$(VERSION)_source.deb:
	(cd $(PROJECT_DIR)/agent; \
	rm -rf deb_dist; \
	export PROJECT_DIR=$(PROJECT_DIR); \
	export VERSION=$(VERSION); \
	export RELEASE=$(RELEASE); \
        python setup.py --command-packages=stdeb.command sdist_dsc --copyright-file $(PROJECT_DIR)/COPYRIGHT --suite $(PPA_DIST); \
        cd deb_dist/f5-bigip-lbaas-agent-$(VERSION); \
        dpkg-buildpackage -rfakeroot -S -k$(GPG_KEY); \
        cd $(PROJECT_DIR)/agent/deb_dist; \
        dput ppa:f5openstackcommunity/havana f5-bigip-lbaas-agent_$(VERSION)-1_source.changes; \
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

