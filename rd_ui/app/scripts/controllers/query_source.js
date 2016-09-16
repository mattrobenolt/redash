(function() {
  'use strict';

  function QuerySourceCtrl(Events, growl, $controller, $scope, $location, $http, Query, Visualization, KeyboardShortcuts) {
    // extends QueryViewCtrl
    $controller('QueryViewCtrl', {$scope: $scope});
    // TODO:
    // This doesn't get inherited. Setting it on this didn't work either (which is weird).
    // Obviously it shouldn't be repeated, but we got bigger fish to fry.
    var DEFAULT_TAB = 'table';

    Events.record(currentUser, 'view_source', 'query', $scope.query.id);

    var isNewQuery = !$scope.query.id,
        queryText = $scope.query.query,
        // ref to QueryViewCtrl.saveQuery
        saveQuery = $scope.saveQuery,
        forkQuery = $scope.forkQuery;

    $scope.sourceMode = true;
    $scope.canEdit = currentUser.canEdit($scope.query) || $scope.query.can_edit;// TODO: bring this back? || clientConfig.allowAllToEditQueries;
    $scope.isDirty = false;
    $scope.base_url = $location.protocol()+"://"+$location.host()+":"+$location.port();

    $scope.newVisualization = undefined;

    // @override
    Object.defineProperty($scope, 'showDataset', {
      get: function() {
        return $scope.queryResult && $scope.queryResult.getStatus() == 'done';
      }
    });

    var shortcuts = {
      'meta+s': function () {
        if ($scope.canEdit) {
          $scope.saveQuery();
        }
      },
      'ctrl+s': function () {
        if ($scope.canEdit) {
          $scope.saveQuery();
        }
      },
      // Cmd+Enter for Mac
      'meta+enter': $scope.executeQuery,
      // Ctrl+Enter for PC
      'ctrl+enter': $scope.executeQuery
    };

    KeyboardShortcuts.bind(shortcuts);

    // @override
    $scope.saveQuery = function(options, data) {
      var savePromise = saveQuery(options, data);

      if (!savePromise) {
        return;
      }

      savePromise.then(function(savedQuery) {
        queryText = savedQuery.query;
        $scope.isDirty = $scope.query.query !== queryText;
        // update to latest version number
        $scope.query.version = savedQuery.version;

        if (isNewQuery) {
          // redirect to new created query (keep hash)
          $location.path(savedQuery.getSourceLink());
        }
      }, function(error) {
        if(error.status == 409) {
          growl.addErrorMessage('It seems like the query has been modified by another user. ' +
            'Please copy/backup your changes and reload this page.', {ttl: -1});
        }
      });

      return savePromise;
    };

    $scope.forkQuery = function(options, data) {
      var savePromise = forkQuery(options, data);

      if (!savePromise) {
        return;
      }

      savePromise.then(function(savedQuery) {
        queryText = savedQuery.query;
      });

      return savePromise;
    };

    $scope.duplicateQuery = function() {
      Events.record(currentUser, 'fork', 'query', $scope.query.id);
      $scope.forkQuery({
        successMessage: 'Query forked',
        errorMessage: 'Query could not be forked'
      }).then(function redirect(savedQuery) {
        // redirect to forked query (clear hash)
        $location.url(savedQuery.getSourceLink()).replace()
      });
    };

    $scope.deleteVisualization = function($e, vis) {
      $e.preventDefault();
      if (confirm('Are you sure you want to delete ' + vis.name + ' ?')) {
        Events.record(currentUser, 'delete', 'visualization', vis.id);

        Visualization.delete(vis, function() {
          if ($scope.selectedTab == vis.id) {
            $scope.selectedTab = DEFAULT_TAB;
            $location.hash($scope.selectedTab);
          }
          $scope.query.visualizations =
            $scope.query.visualizations.filter(function (v) {
              return vis.id !== v.id;
            });
        }, function () {
          growl.addErrorMessage("Error deleting visualization. Maybe it's used in a dashboard?");
        });
      }
    };

    $scope.$watch('query.query', function(newQueryText) {
      $scope.isDirty = (newQueryText !== queryText);
    });

    $scope.$on('$destroy', function destroy() {
      KeyboardShortcuts.unbind(shortcuts);
    });
  }

  angular.module('redash.controllers').controller('QuerySourceCtrl', [
    'Events', 'growl', '$controller', '$scope', '$location', '$http',
    'Query', 'Visualization', 'KeyboardShortcuts', QuerySourceCtrl
    ]);
})();
