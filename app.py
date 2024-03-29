import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
import pld
import datetime
import pytz
import flask
from flask import request, jsonify
import schedule
import threading
import time

app = flask.Flask(__name__)

cred = credentials.Certificate('credentials.json')
firebase_admin.initialize_app(cred)

db = firestore.client()

users_ref = db.collection('users')


# tool functions
def get_access_token(uid):
    return users_ref.document(uid).collection('account').document('account_data').get().to_dict()['access_token']

def read_ac(uid):
    return users_ref.document(uid).collection('account').document('account_data').get().to_dict()

def write_ac(uid, field_name, update_value):
    users_ref.document(uid).collection('account').document('account_data').update({str(field_name): update_value})



#create end point
def create_new_user(user_id, mb, tab, start_date):
    try:
       user_ref = users_ref.document(user_id)
       #create a new collection called account with three fields - monthly budget, tab, start date (datetimeobject)
       start_date = datetime.datetime.strptime(start_date, '%Y-%m-%d %H:%M:%S.%f')
       user_ref.collection('account').document('account_data').set({
           'monthly_budget': mb,
           'tab': tab,
           'start_date': start_date,
           'end_date': start_date + datetime.timedelta(days=30),
           'access_token': ""
       })

    except Exception as e:
        return jsonify({"error": e}), 404

    else:
        return jsonify({"message": True}), 200

# refresh account endpoint
def refresh_account(uid):
    #output required: day balance & runway
    #first output: day balance
    #check whether a document field called account_balance exists

    access_token = get_access_token(uid)

    if not 'account_balance' in users_ref.document(uid).collection('account').document('account_data').get().to_dict():
        #if not, create missing fields for new user
        users_ref.document(uid).collection('account').document('account_data').update({
            'account_balance': pld.get_real_balance(access_token),
            'added_savings': 0,
            'savings': 0,
            'last_change_to_tab': 0
        })
    
    #calculate money spent from last time
    try: 
       new_balance = pld.get_real_balance(access_token)

    except Exception as e:
        print(e)
        return jsonify({"message": False}), 404

    else: 
       spent = read_ac(uid=uid)['account_balance'] - new_balance
       #create a field last_change_to_tab if it doesn't exist
       if not 'last_change_to_tab' in users_ref.document(uid).collection('account').document('account_data').get().to_dict():
              users_ref.document(uid).collection('account').document('account_data').update({
                'last_change_to_tab': 0
              })

       #write spent amount as last_change_to_tab in firebase
       if read_ac(uid=uid)['last_change_to_tab'] > 0:
          pass

       else:
          users_ref.document(uid).collection('account').document('account_data').update({
           'last_change_to_tab': spent
          })
    
       #write tab
       tab = read_ac(uid=uid)['tab']
       tab += spent
       write_ac(uid=uid, field_name='tab', update_value=tab)

       #calculate ideal spend: date_limit + added savings
       #calculate days_running, the number of the day it is from the beginning of the start date from account_data plus 1 day
       start_date = read_ac(uid=uid)['start_date']
       days_running = (datetime.datetime.now(pytz.UTC) - start_date).days + 1 

       #calculate daily limit, which is monthly budget divided by 30
       daily_limit = read_ac(uid=uid)['monthly_budget'] / 30 #change to days in period

       write_ac(uid=uid, field_name='day_add', update_value=daily_limit)

       #calculate ideal spend, which is daily limit multiplied by days running
       ideal_spend = daily_limit * days_running + read_ac(uid=uid)['added_savings']

       #calculate day balance, which is ideal spend minus tab
       day_balance = ideal_spend - tab

       #write day balance
       write_ac(uid=uid, field_name='day_balance', update_value=day_balance)

       write_ac(uid=uid, field_name='account_balance', update_value=new_balance)

       #second output: runway

       if days_running == 1:
           avg_spending_rate = 0
       #calculate runway_days
       #calculate average spending rate per day, which is tab divided by (days running -1)
       else:
           avg_spending_rate = tab / (days_running - 1)
    

       #calculate max days, which is monthly budget minus tab divided by average spending rate
       if avg_spending_rate == 0:
           max_days = 0
    
       else:
           max_days = (read_ac(uid=uid)['monthly_budget'] - read_ac(uid=uid)['tab']) / avg_spending_rate
       #calculate runway_days, which is max days minus days running minus 31
       runway_days = max_days - days_running + 31 #days_in_period + 1
    
       if runway_days >= 0:
           write_ac(uid=uid, field_name='runway', update_value="On track")

       else:
           write_ac(uid=uid, field_name='runway', update_value=str(runway_days))

       return jsonify({"message": True}), 200

def update_mb(uid, new_mb):
    old_mb = read_ac(uid=uid)['monthly_budget']
    write_ac(uid=uid, field_name='monthly_budget', update_value=new_mb)
    try:
      refresh_account(uid=uid)

    except Exception as e:
        write_ac(uid=uid, field_name='monthly_budget', update_value=old_mb)
        return jsonify({"error": f"{e}"}), 400

    else:
        return jsonify({"message": True}), 200

def update_start_date(uid, new_start_date, new_tab):
    old_start_date = read_ac(uid=uid)['start_date']
    write_ac(uid=uid, field_name='start_date', update_value=new_start_date)
    old_tab = read_ac(uid=uid)['tab']
    write_ac(uid=uid, field_name='tab', update_value=new_tab)
    try:
      refresh_account(uid=uid)

    except Exception as e:
        write_ac(uid=uid, field_name='start_date', update_value=old_start_date)
        write_ac(uid=uid, field_name='tab', update_value=old_tab)
        return jsonify({"error": "Could not refresh account"}), 400

    else:
        return jsonify({"message": True}), 200

def exclude_transaction(uid, transaction_amount):
    old_tab = read_ac(uid=uid)['tab']
    new_tab = old_tab + transaction_amount
    write_ac(uid=uid, field_name='tab', update_value=new_tab)
    try:
      refresh_account(uid=uid)

    except Exception as e:
        write_ac(uid=uid, field_name='tab', update_value=old_tab)
        return jsonify({"error": "Could not refresh account"}), 400

    else:
        return jsonify({"message": True}), 200

def update_savings(uid, savings_addition, action):
    old_savings = read_ac(uid=uid)['savings']
    new_savings = old_savings
    ad_s = read_ac(uid=uid)['added_savings']
    new_ad_s = ad_s
    if action == "add":
        new_savings = old_savings + savings_addition
        new_ad_s = ad_s - savings_addition
    elif action == "reduce":
        new_savings = old_savings - savings_addition
        new_ad_s = ad_s + savings_addition

    write_ac(uid=uid, field_name='savings', update_value=new_savings)
    write_ac(uid=uid, field_name='added_savings', update_value=new_ad_s)
    try:
      refresh_account(uid=uid)

    except Exception as e:
        write_ac(uid=uid, field_name='savings', update_value=old_savings)
        return jsonify({"error": "Invalid JSON data"}), 400

    else:
        return jsonify({"message": True}), 200

#new period function
def reset_budget():
    #check if it is over 30 days since the start date
    #if yes, reset tab, start date and trigger refresh_account
    #if no, do nothing
    #trigger check new period every day
    for user in users_ref.stream():
        uid = user.id
        resp = refresh_account(uid=uid)
        start_date = read_ac(uid=uid)['start_date']
        if (datetime.datetime.now(pytz.UTC) - start_date).days >= 30:
            if read_ac(uid=uid)['day_balance'] > 0:
                #added savigns = added savings + day balance
                added_savings = read_ac(uid=uid)['added_savings']
                added_savings += read_ac(uid=uid)['day_balance']
                write_ac(uid=uid, field_name='added_savings', update_value=added_savings)
                #reset day balance
                write_ac(uid=uid, field_name='day_balance', update_value=0)
            
            else:
                #reset day balance
                write_ac(uid=uid, field_name='day_balance', update_value=0)
        

            write_ac(uid=uid, field_name='tab', update_value=0)
            write_ac(uid=uid, field_name='start_date', update_value=datetime.datetime.now(pytz.UTC))
            refresh_account(uid=uid)

#refresh_account('42xGhEiG9Fe0YZ2DAOBoFHTDEYF2')

@app.route('/create_account', methods = ['POST'])
def create_doc_route():
    #code to read uid from json body
    data = request.get_json()

    if not data:
        return jsonify({"error": "Invalid JSON data"}), 400

    uid = data.get("user_ref")
    mb = data.get("monthly_budget")
    tab = data.get("tab")
    start_date = data.get("start_date")
    response = create_new_user(user_id=uid, mb=mb, tab=tab, start_date=start_date)
    return response

@app.route('/refresh_account', methods = ['POST'])
def refresh_account_route():
    data = request.get_json()

    if not data:
        return jsonify({"error": "Invalid JSON data"}), 400

    uid = data.get("user_ref")
    response = refresh_account(uid=uid)
    return response

@app.route('/update_mb', methods = ['POST'])
def update_mb_route():
    data = request.get_json()

    if not data:
        return jsonify({"error": "Invalid JSON data"}), 400

    uid = data.get("user_ref")
    new_mb = data.get("new_mb")
    response = update_mb(uid=uid, new_mb=new_mb)
    return response

@app.route('/update_start_date', methods = ['POST'])
def update_start_date_route():
    data = request.get_json()

    if not data:
        return jsonify({"error": "Invalid JSON data"}), 400

    uid = data.get("user_ref")
    new_start_date = data.get("start_date")
    new_tab = data.get("tab")
    #convert string to date object
    new_start_date = datetime.datetime.strptime(new_start_date, '%Y-%m-%d %H:%M:%S.%f')
    response = update_start_date(uid=uid, new_start_date=new_start_date, new_tab=new_tab)
    return response

@app.route('/update_savings', methods = ['POST'])
def update_savings_route():
    data = request.get_json()

    if not data:
        return jsonify({"error": "Invalid JSON data"}), 400

    uid = data.get("user_ref")
    savings_addition = data.get("change")
    action = data.get("action")
    response = update_savings(uid=uid, savings_addition=savings_addition, action=action)
    return response

@app.route('/exclude_transaction', methods = ['POST'])
def exclude_transaction_route():
    data = request.get_json()

    if not data:
        return jsonify({"error": "Invalid JSON data"}), 400

    uid = data.get("user_ref")
    transaction_amount = data.get("transaction_amount")
    response = exclude_transaction(uid=uid, transaction_amount=transaction_amount)
    return response

# tool functions
schedule.every().day.at("00:00").do(reset_budget)

def run_schedule():
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == '__main__':
    flask_thread = threading.Thread(target=app.run(debug=True, host = '77.68.119.174'), kwargs={'debug': True})
    flask_thread.start()

    schedule_thread = threading.Thread(target=run_schedule)
    schedule_thread.start()       

#code to trigger check new period function every 24 hours
#

