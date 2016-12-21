;(function(d3) {	
	"use strict";
	owid.namespace("owid.chart");

	owid.chart = function() {
		var chart = owid.dataflow();
		window.chart = chart;

		// Set up models and data processors
		App.VariableData = new App.Models.VariableData();	
		App.ChartData = new App.Models.ChartData();
		App.Colors = new App.Models.Colors();
		App.ChartModel.bind();
		chart.model = App.ChartModel;
		chart.vardata = App.VariableData;
		chart.data = App.ChartData;
		chart.map = App.MapModel;
		chart.colors = App.Colors;

		chart.requires('containerNode', 'outerBounds', 'activeTabName');

		chart.defaults({
			authorWidth: App.AUTHOR_WIDTH,
			authorHeight: App.AUTHOR_HEIGHT
		});

		chart.initial('header', function() { return owid.control.header(chart); });
		chart.initial('creditsFooter', function() { return new App.Views.Chart.Footer(chart); });
		chart.initial('controlsFooter', function() { return owid.view.controlsFooter(); });

		// Container setup
		chart.flow('containerNode', function(containerNode) {
			d3.select(containerNode).classed('chart-container', true);
		});
		chart.flow('el : containerNode', function(containerNode) {
			return d3.select(containerNode).append('div').attr('id', 'chart');
		});
		chart.flow('el, outerBounds', function(el, outerBounds) {
		});
		chart.flow('dom : el', function(el) {
			return el.node();
		});
		chart.flow('htmlNode : el', function(el) {
			return el.node();
		});
		chart.flow('svg : el', function(el) {
			return el.append('svg').attr('xmlns', 'http://www.w3.org/2000/svg').attr('xmls:xlink', 'http://www.w3.org/1999/xlink').attr('version', '1.1');
		});

		// Tabs setup
		chart.initial('tabs', function() {
			return {
				chart: owid.tab.chart(chart),
				data: owid.component.dataTab(chart),
				map: owid.tab.map(chart),
				sources: owid.component.sourcesTab(chart),
				share: owid.component.shareTab(chart)
			};
		});
		chart.flow('activeTab : tabs, activeTabName', function(tabs, activeTabName) {
			var tab = tabs[activeTabName];

			if (chart.activeTab && (!tab.isOverlay || chart.activeTab.isOverlay))
				chart.activeTab.clean();

			return tab;
		});

		chart.flow('activeTab, outerBounds', function() { chart.data.ready(chart.render); });


		// Scaling setup
		chart.flow('innerBounds : authorWidth, authorHeight', function(authorWidth, authorHeight) {
			return owid.bounds(0, 0, authorWidth, authorHeight).pad(15);
		});
		chart.flow('scale : outerBounds, authorWidth, authorHeight', function(outerBounds, authorWidth, authorHeight) {
			return Math.min(outerBounds.width/authorWidth, outerBounds.height/authorHeight);
		});
		chart.flow('el, authorWidth, authorHeight, scale', function(el, authorWidth, authorHeight, scale) {
			el.style('width', authorWidth*scale + 'px').style('height', authorHeight*scale + 'px')
			  .style('display', 'inline-block').style('vertical-align', 'middle');
		});
		chart.flow('svg, authorWidth, authorHeight, scale', function(svg, authorWidth, authorHeight, scale) {
			svg.style('width', '100%')
			   .style('height', '100%')
			   .attr('viewBox', '0 0 ' + authorWidth + ' ' + authorHeight);
		});

		chart.render = function() {
			chart.now('el, header, controlsFooter, creditsFooter, activeTab, innerBounds, scale', function(el, header, controlsFooter, creditsFooter, activeTab, innerBounds, scale) {
				chart.data.transformData();
				var bounds = innerBounds;

/*				var paddingLeft = 50,
					paddingTop = 50;

				var bounds = { left: paddingLeft, top: paddingTop, width: chart.innerRenderWidth-(paddingLeft*2), height: chart.innerRenderHeight-(paddingTop*2) };*/

				header.render(bounds);

				bounds = bounds.padTop(header.view.bbox.height);

				controlsFooter.render(bounds);

				bounds = bounds.padBottom(controlsFooter.height/scale);

	//			owid.boundsDebug(bounds);
				creditsFooter.render(bounds);

				bounds = bounds.padBottom(creditsFooter.height);

				if (activeTab.isOverlay)
					activeTab.render(innerBounds.padBottom(controlsFooter.height/scale));
				else
					activeTab.render(bounds);

				el.select('.chart-inner').style('visibility', 'visible');

				d3.selectAll(el.node().childNodes).filter(function() { return this.nodeName != 'svg'; }).style('font-size', scale*16 + 'px');
			});
		};

		chart.setupDOM = function() {
			jQuery(window).resize(chart.resize);

/*			jQuery(window).one("chart-loaded", function() {
				chart.onResize(function() {
					window.top.postMessage("chartLoaded", "*");
					console.log("Loaded chart: " + chart.model.get("chart-name"));
				});
			});*/

			// Pass through touch events to containing document
			// Important for presentations on mobile
			jQuery('body').on('touchstart', function(ev) {
				var touches = [];
				_.each(ev.originalEvent.touches, function(touch) {
					touches.push({
						clientX: touch.clientX,
						clientY: touch.clientY
					});
				});
				window.parent.postMessage({ event: 'touchstart', touches: touches }, "*");
			});

			jQuery('body').on('touchmove', function(ev) {
				if ($(ev.target).closest('.map-timeline-controls').length) return;
				
				var touches = [];
				_.each(ev.originalEvent.touches, function(touch) {
					touches.push({
						clientX: touch.clientX,
						clientY: touch.clientY
					});
				});
				window.parent.postMessage({ event: 'touchmove', touches: touches }, "*");
			});

			jQuery('body').on('touchend', function(ev) {
				window.parent.postMessage({ event: 'touchend' }, "*");
			});

			jQuery(document).ajaxStart(function() {
				d3.select('.chart-preloader').classed('hidden', false);
			});

			jQuery(document).ajaxStop(function() {
				d3.select('.chart-preloader').classed('hidden', true);
			});

			if (chart.model.get("chart-name"))
				d3.select('.chart-preloader').classed('hidden', false);

			if (window.self != window.top || App.isEditor) {
				$chart.addClass("embedded");
			}
		};

		// HACK (Mispy): Workaround for the differences in getBoundingClientRect
		// depending on whether you're using zoom or transform
		chart.getBounds = function(node) {
			var bounds = node.getBoundingClientRect(),
				untransformedBounds;

			if (chart.scale > 1 && owid.features.zoom) {
				untransformedBounds = bounds;
			} else {
				untransformedBounds = {
					top: bounds.top / chart.scale,
					right: bounds.right / chart.scale,
					bottom: bounds.bottom / chart.scale,
					left: bounds.left / chart.scale,
					height: bounds.height / chart.scale,
					width: bounds.width / chart.scale
				};
			}
			return untransformedBounds;
		};

		chart.getTransformedBounds = function(node) {
			var bounds = node.getBoundingClientRect(),
				transformedBounds;

			if (chart.scale > 1 && owid.features.zoom) {
				transformedBounds = {
					top: bounds.top * chart.scale,
					right: bounds.right * chart.scale,
					bottom: bounds.bottom * chart.scale,
					left: bounds.left * chart.scale,
					height: bounds.height * chart.scale,
					width: bounds.width * chart.scale
				};
			} else {
				transformedBounds = bounds;
			}

			return transformedBounds;			
		};

		chart.resize = function() {
			chart.now('containerNode', function(containerNode) {
				var bounds = containerNode.getBoundingClientRect();				
				var marginLeft = bounds.width*0.2, marginTop = bounds.height*0.1;

				chart.update({
					outerBounds: owid.bounds(bounds).pad(marginLeft, marginTop)
				});
			});
		};

		chart.handleError = function(err, isCritical) {
			if (isCritical !== false) isCritical = true;

			if (err.responseText) {
				err = err.status + " " + err.statusText + "\n" + "    " + err.responseText;
			} else if (err.stack) {
				err = err.stack;
			}
			console.error(err);
			var tab = this.activeTab || this.loadingTab;
			if (tab)
				tab.deactivate();
			this.activeTab = null;
			this.loadingTab = null;
			this.$(".chart-preloader").hide();
			if (isCritical) {
				this.$(".tab-pane.active").prepend('<div class="chart-error critical"><pre>' + err + '</pre></div>');
			} else {
				this.showMessage(err);
			}
		};

		chart.showMessage = function(msg) {
			this.el.select(".tab-pane.active").append('<div class="chart-error"><div>' + msg + '</div></div>');			
		};

		chart.update({ containerNode: d3.select('body').node() });
		chart.setupDOM();
		chart.resize();

		// For events that MUST be asynchronous and global, use sparingly
		chart.dispatch = d3.dispatch('renderEnd');

		chart.mapdata = owid.models.mapdata(chart);
		// DOM setup

		// Initialize components
		chart.url = owid.view.urlBinder(chart);
		chart.exporter = new App.Views.Export(chart);
		chart.tabSelector = owid.view.tabSelector(chart);
		chart.debugHelper = new App.Views.DebugHelper(chart);
		chart.tooltip = new owid.view.tooltip(chart);

		chart.model.on('change', function() {
			chart.data.ready(chart.render);
		});
		chart.map.on('change', function() {
			chart.data.ready(chart.render);
		});
		chart.data.ready(chart.render);

		return chart;
	};	
})(d3v4);