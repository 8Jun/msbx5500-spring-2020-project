from flask import Flask, request, render_template, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_pymongo import PyMongo
import gridfs
import os
import subprocess
import datetime
from io import BytesIO
import pandas as pd
from bson.objectid import ObjectId
import pickle as pkl
import sklearn
import json

app = Flask(__name__)
# add cross-origin allow to all routes
CORS(app)

original_dataset_features = ['duration',
 'protocol_type',
 'service',
 'flag',
 'src_bytes',
 'dst_bytes',
 'land',
 'wrong_fragment',
 'urgent',
 'hot',
 'num_failed_logins',
 'logged_in',
 'lnum_compromised',
 'lroot_shell',
 'lsu_attempted',
 'lnum_root',
 'lnum_file_creations',
 'lnum_shells',
 'lnum_access_files',
 'lnum_outbound_cmds',
 'is_host_login',
 'is_guest_login',
 'count',
 'srv_count',
 'serror_rate',
 'srv_serror_rate',
 'rerror_rate',
 'srv_rerror_rate',
 'same_srv_rate',
 'diff_srv_rate',
 'srv_diff_host_rate',
 'dst_host_count',
 'dst_host_srv_count',
 'dst_host_same_srv_rate',
 'dst_host_diff_srv_rate',
 'dst_host_same_src_port_rate',
 'dst_host_srv_diff_host_rate',
 'dst_host_serror_rate',
 'dst_host_srv_serror_rate',
 'dst_host_rerror_rate',
 'dst_host_srv_rerror_rate']

# convenience class for throwing api errors in valid format
# https://flask.palletsprojects.com/en/1.1.x/patterns/apierrors/
class InvalidUsage(Exception):
    status_code = 400

    def __init__(self, message, status_code=None, payload=None):
        Exception.__init__(self)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        self.payload = payload

    def to_dict(self):
        rv = dict(self.payload or ())
        rv['message'] = self.message
        return rv

@app.errorhandler(InvalidUsage)
def handle_invalid_usage(error):
    response = jsonify(error.to_dict())
    response.status_code = error.status_code
    return response

########
### PSQL
########
# get psql db up. quickstart here: https://flask-sqlalchemy.palletsprojects.com/en/2.x/quickstart/
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS']=False
db = SQLAlchemy(app)

class Alert(db.Model):
    '''
    look at documentation for flask_sqlalchemy and for SQLAlchemy
    '''
    __tablename__ = "alertsDb"
    id = db.Column(db.Integer, primary_key=True, nullable= False)
    StartTime=db.Column(db.String(30))
    Dur=db.Column(db.Float)
    Proto=db.Column(db.String(7))
    SrcAddr= db.Column(db.String(39), nullable = False)
    Sport=db.Column(db.String(10), nullable = False)
    Dir=db.Column(db.String(10))
    DstAddr=db.Column(db.String(39), nullable = False)
    Dport=db.Column(db.String(10), nullable = False)
    State=db.Column(db.String(15))
    sTos=db.Column(db.Float)
    dTos=db.Column(db.Float)
    TotPkts=db.Column(db.Integer)
    TotBytes=db.Column(db.Integer)
    SrcBytes=db.Column(db.Integer)
    Predictions=db.Column(db.String)
    reso=db.Column(db.String(1), server_default = '0')
    time_resolved=db.Column(db.DateTime, onupdate=datetime.datetime.now())

###########
### MONGODB
###########
# get a mongodb up
# quickstart https://flask-pymongo.readthedocs.io/en/latest/
app.config["MONGO_URI"] = os.getenv('MONGODB_URI')
mongo = PyMongo(app)
fs = gridfs.GridFS(mongo.db) # direct access to the gridfs file system
                             # within the mongo database

@app.route('/')
def dashboard():
    return render_template('dashboard.html', **thresholds)



############
## Files
#############
'''
the code below should only allow pcap files
'''
#allowed_extensions = {'pcap'}
#def allowed_file(filename):
#    return '.' in filename and \
#        filename.rsplit('.',1)[1].lower() in allowed_extensions

@app.route('/files/<path:filename>', methods=['POST'])
def upload_file(filename):
    '''
    The file will (should) be available under the `request.files` key,
    where `request` is a flask object provided to the route
    '''
    the_file = request.files['the_file']
    mongo.save_file(filename, the_file)
    return ('', 204)

class InvalidUsageError(Exception):
    pass

thresholds = {
    'kddcup_threshold': 0.6, # use later
    'ctu_13_threshold': 0.255506 # use later
}

# load ctu-13 model
path = './RandomForestClassifier.pkl'
with open(path, 'rb') as f:
    model_ctu_13 = pkl.load(f)

# load kddcup model
path = './LogisticRegression.pkl'
with open(path, 'rb') as f:
    model_kddcup = pkl.load(f)

def _get_ctu_13_predictions(net_flows):
    net_flows= net_flows.iloc[:,0:14]
    return model_ctu_13.predict_proba(net_flows)[:,1]

def _get_kddcup99_predictions(net_flows):
    '''
    should return the output of predict_proba, nparray
    '''

    #do Kddcup predictions
    feature_names = [str(col) for col in net_flows.columns]

    error = None
    y_score_kdd = None
    predict_me = {feature_name: None for feature_name in original_dataset_features}

    # override the features we actually care about with ones submitted by the form.
    for feature_name in feature_names:
       # submitted_val = net_flows[feature_name]
       # if not submitted_val:
           #raise InvalidUsageError('missing feature {}'.format(feature_name))
        predict_me[feature_name] = net_flows[feature_name]

    predict_me = pd.DataFrame(predict_me)
    y_score_kdd = model_kddcup.predict_proba(predict_me)[:,1]
    return y_score_kdd

def _threshold_filter_predictions(predictions):
    '''
    prediction: a dictionary (row) of predictions, one column (key) for each prediction

    Create a new series storing predictions which surpass some arbitrary threshold.
    '''
    alert_me = {}
    for model, proba in predictions.items():
        if proba > thresholds['{}_threshold'.format(model)]:
            alert_me[model] = proba
    if not alert_me:
        return None
    return alert_me

@app.route('/files/process/<string:_id>', methods=['GET'])
def process_file(_id):
    '''
    This should fetch the hopefully-pcap file from GridFS, parse it into NetFlow
    records, and run each record against your sklearn model(s). If the prediction
    is above :shurg: some threshold, save the record (along with the prediction
    threshold? and maybe the model that made the prediction?) to the Alerts table.

    Feel free to write smaller helper functions  that this function in turn calls.
    (Please write helper functions. This is way too much for just one function.)

    the below example reads from an example `example_capture.pcap` file.
    But you will need to modify the code to read from stdin, instead. (e.g., `-r -`).
    Then, pass in a file as the `input` argument to the `check_output` call. This
    will be read by the subprocess as stdin.

    e.g., you will need to do:
        net_flows_bytes = subprocess.check_output('argus -F argus.conf -r - -w - | ra -r - -n -F ra.conf -Z b',
            input=fs.find_one({"filename": "lisa.txt"}),
            shell=True)
    '''
    '''
    From GOOGLE DOC:
    def process_file():
	Parse a pcap into netflows
	Ask a model to evaluate (make predictions for) each netflow record
	For each netflow_plus_prediction:
		If netflow_plus_prediction[prediction] > threshold:
			New_alert = Alert(netflow_plus_prediction)
			db.add(new_alert)

		db.commit()
    '''

    # Get file to process and parse to netflows
    file = fs.get(ObjectId(_id)).read()
    net_flows_bytes = subprocess.check_output('argus -F argus.conf -r - -w - | ra -r - -n -F ra.conf -Z b',
            input=file,
            shell=True)

    net_flows_bytesIO = BytesIO(net_flows_bytes)
    net_flows = pd.read_csv(net_flows_bytesIO)
    net_flows = net_flows.drop('Label', axis=1)
    net_flows = net_flows.fillna(0)

    y_score_ctu = _get_ctu_13_predictions(net_flows)
    y_score_kddcup = _get_kddcup99_predictions(net_flows)

    predictions = []
    for i in range(net_flows.shape[0]):
        _single_net_flow_predictions = {
            'ctu_13': y_score_ctu[i],
            'kddcup': y_score_kddcup[i]
        }
        _thresholded_predictions = _threshold_filter_predictions(_single_net_flow_predictions)
        predictions.append(json.dumps(_thresholded_predictions) if _thresholded_predictions else None)

    net_flows['Predictions'] = pd.Series(predictions)
    # If row is above threshold, commit that row to the DB
    to_alerts = net_flows[net_flows['Predictions'].notnull()]

    to_alerts = to_alerts.head() # lol dirty hack
    to_alerts.to_sql(name='alertsDb', con=db.engine, if_exists = 'append', index=False)
    db.session.commit()
    return ('', 204)

@app.route('/files.json', methods=['GET'])
def list_files():
    '''
    Do a "find_all" of some sort against your mongo gridfs object.
    Make sure that you're at least returning the `_id` of each file;
    will be important for the rendering of the action buttons for
    our "files" html table.

    Note: The direct return of a return from a gridfs query is not
    json-serializable because it includes _dadgum files_.
    '''

    files = list(fs.find())
    return jsonify([{'filename': file.name,'_id': str(file._id)} for file in files])


############
## Alerts
############

@app.route('/alerts.json', methods=['GET'])
def list_alerts():
    '''
    Do some kind of find_all against your Alert class. Return the list. Make
    sure that you return some kind of `id` for each alert. Will be important
    for rendering the action buttons for our HTML table of alerts.

    See "resolve_alert" function. Perhaps filter to only return alerts that are
    not flagged as "resolved."
    '''

    alerts = [{'_id':i.id, 'n_packet': i.TotPkts,
                'src_bytes':i.TotBytes, 'src_addr':i.SrcAddr, 'dst_addr': i.DstAddr, 'Protocol':i.Proto,
                'Timestamp':i.StartTime, 'Predictions':i.Predictions }
                for i in Alert.query.filter_by(reso='0').all() ]

    return jsonify(alerts)

@app.route('/alerts/<string:_id>', methods=['DELETE'])
def resolve_alert(_id):
    '''
    Ha, maybe don't actually "delete" it per se. Perhaps just "flag" it as deleted.
    (Would require an additional column in the Alerts table, something like
    "is_deleted", perhaps also with a timestamp for when it was deleted).
    Does it hurt your sensibilities to not actually "delete" something from a
    database yet pretend that it is deleted? Welcome to Ashley Madison. Else, how
    else could you "undelete" something?
    '''
    update=Alert.query.filter_by(id = _id).first()
    update.reso='1'
    db.session.commit()
    return ('', 204)
