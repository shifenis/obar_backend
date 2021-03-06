from datetime import datetime as dt

from flask import request
from flask_restplus import Resource, Namespace, fields
from werkzeug.exceptions import NotFound, UnprocessableEntity, Forbidden, InternalServerError

from obar.models import Customer, Purchase, PurchaseItem, Product
from obar.models import db
from sqlalchemy.exc import OperationalError
from .decorator.auth_decorator import customer_token_required, admin_token_required
from .marshal.fields import purchase_item_fields, operation_purchase_leaderboard_fields, operation_best_selling_fields, \
    operation_check_gift_fields
from .service.operation_service import purchase_leaderboard, best_selling_product, \
    produce_expenses, produce_purchase_list, recent_purchases, gift_purchase, undo_purchase

authorizations = {
    "JWT": {
        "type": "apiKey",
        "in": "header",
        "name": "Authorization"
    }
}

operation_ns = Namespace('operation', description='Common operations', authorizations=authorizations)

purchase_item_model = operation_ns.model('Purchase Item', purchase_item_fields)
purchase_details_model = operation_ns.model('Product details', {
    'product_code': fields.String(required=True,
                                  description='Product code'),
    'purchase_quantity': fields.Integer(required=True,
                                        description='Item quantity')
})

perform_purchase_model = operation_ns.model('Purchase Order', {
    'purchase_details': fields.List(fields.Nested(purchase_details_model))
})

produce_expense_nested_model = operation_ns.model('Purchase Nested', {
    'date': fields.Date(description='Purchase date'),
    'code': fields.String(description='Purchase UUID'),
    'cost': fields.Float(description='Purchase cost')
})

operation_produce_expenses_model = operation_ns.model('Expense Review', {
    'customer': fields.String(description='Customer mail address'),
    'total_expenses': fields.Float(description='Total purchases costs'),
    'purchases': fields.List(fields.Nested(produce_expense_nested_model))
})

operation_produce_purchase_model = operation_ns.model('Purchase List', {
    'date': fields.Date(description='Purchase date'),
    'items': fields.List(fields.Nested(purchase_item_model))
})
operation_purchase_leaderboard_model = operation_ns.model('Purchase Chart', operation_purchase_leaderboard_fields)
operation_best_selling_model = operation_ns.model('Best Selling', operation_best_selling_fields)
operation_check_gift_model = operation_ns.model('Check Gift', operation_check_gift_fields)

@operation_ns.route('/purchaseProducts')
class OperationAPI(Resource):

    @operation_ns.doc('post_purchase_products', security='JWT')
    @operation_ns.response(200, description='The purchase has been performed')
    @operation_ns.response(500, description='Errors in db')
    @operation_ns.response(404, description='Could not found customer')
    @operation_ns.response(422, description='Request is semantically correct but cannot be processed')
    @operation_ns.expect(perform_purchase_model, validate=True)
    @customer_token_required
    def post(self):
        """
        Performs a purchase operation
        """
        data = Customer.decode_auth_token(request.headers['Authorization'])
        try:
            customer = Customer.query.filter_by(customer_mail_address=data['customer']).first()
        except OperationalError:
            raise InternalServerError('Customer table is missing')
        purchase = Purchase(purchase_date=dt.utcnow(),
                            purchase_customer_mail_address=customer.customer_mail_address)
        if customer is None:
            raise NotFound(description='Resource ' + customer.__repr__() + ' is not found')

        # check if the product is requested more than once
        product_list = set()
        for details in request.json['purchase_details']:
            # adds the product to a set to later check if the product
            # appears more than once in the purchase_details
            if details['purchase_quantity'] <= 0:
                raise UnprocessableEntity('Purchase quantity cannot be <= 0')
            product_list.add(details['product_code'])

        # If length of set and received entries are different then
        # it means some products shows up twice in the list,
        # raise 422 HTTP error
        if len(product_list) != len(request.json['purchase_details']):
            raise UnprocessableEntity('A product has been submitted twice')

        # adds the purchase to session
        db.session.add(purchase)
        for details in request.json['purchase_details']:
            product = Product.query.filter_by(product_code_uuid=details['product_code']).first()
            if product is None:
                raise NotFound('Product ' + details['product_code'] + ' not found')
            if not product.product_availability:
                db.session.remove()
                raise UnprocessableEntity('Unavailable product selected')
            if product.product_quantity <= 0:
                db.session.remove()
                raise UnprocessableEntity('Product out of stock')
            if product.product_quantity < details['purchase_quantity']:
                db.session.remove()
                raise UnprocessableEntity('Too much quantity requested')
            # update the product quantity
            product.product_quantity = product.product_quantity - details['purchase_quantity']
            # create a new association object between a purchase and a product
            purchase_item = PurchaseItem(purchase_item_product_code_uuid=product.product_code_uuid,
                                         purchase_item_purchase_code_uuid=purchase.purchase_code_uuid,
                                         purchase_item_quantity=details['purchase_quantity'])
            db.session.add(purchase_item)
        db.session.commit()
        return {'purchase_uuid': purchase.purchase_code_uuid}, 200


@operation_ns.route('/purchaseLeaderboard')
class OperationPurchaseChartAPI(Resource):

    @customer_token_required
    @operation_ns.doc('post_purchase_chart', security='JWT')
    @operation_ns.marshal_list_with(operation_purchase_leaderboard_model)
    @operation_ns.response(200, description='Success')
    @operation_ns.response(500, description='Internal Server Error')
    def post(self):
        """
        Returns a sorted list of purchases by customer
        """
        return purchase_leaderboard(), 200


@operation_ns.route('/bestProducts')
class OperationBestProductAPI(Resource):

    @customer_token_required
    @operation_ns.doc('post_best_products', security='JWT')
    @operation_ns.response(200, description='Success')
    @operation_ns.marshal_list_with(operation_best_selling_model)
    def post(self):
        """
        Returns the best-selling product
        """
        return best_selling_product()


@operation_ns.route("/produceExpensesReport")
class OperationProduceExpenses(Resource):

    @admin_token_required
    @operation_ns.doc('post_produce_expense', security='JWT')
    @operation_ns.response(200, description='Success')
    @operation_ns.response(500, description='Internal Server Error')
    @operation_ns.marshal_list_with(operation_produce_expenses_model)
    def post(self):
        """
        Produce the expense bill
        """
        return produce_expenses()


@operation_ns.route('/producePurchasesList/<string:mail_address>')
class OperationProducePurchaseList(Resource):

    @customer_token_required
    @operation_ns.doc('post_produce_purchase_report', security='JWT')
    @operation_ns.response(200, description='Success')
    @operation_ns.response(500, description='Internal Server Error')
    @operation_ns.response(403, description='Forbidden')
    @operation_ns.marshal_list_with(operation_produce_purchase_model)
    def post(self, mail_address):
        """
        Produce the expense bill
        """
        data = Customer.decode_auth_token(request.headers['Authorization'])
        if data['customer'] != mail_address:
            raise Forbidden("You don't have the permission to access the requested resource")
        return produce_purchase_list(mail_address)


@operation_ns.route('/recentPurchase')
class OperationRecentPurchase(Resource):

    @operation_ns.doc('post_recent_purchase')
    @operation_ns.response(200, description='Success')
    @operation_ns.response(500, description='Internal Server Error')
    def post(self):
        """
        Show the most recent purchases
        """
        return recent_purchases()


@operation_ns.route('/giftPurchase/<string:purchase_uuid>')
class OperationGiftPurchase(Resource):

    @customer_token_required
    @operation_ns.doc('gift_purchase', security='JWT')
    @operation_ns.response(204, description='No Content')
    @operation_ns.response(404, description='The resource may have been already gifted')
    @operation_ns.response(500, description='Internal Server Error')
    def post(self, purchase_uuid):
        data = Customer.decode_auth_token(request.headers['Authorization'])
        return gift_purchase(purchase_uuid, data['customer'])


@operation_ns.route('/undoPurchase/<string:purchase_uuid>')
class OperationUndoPurchase(Resource):

    @customer_token_required
    @operation_ns.doc('undo_purchase', security='JWT')
    @operation_ns.response(204, description='No Content')
    @operation_ns.response(404, description='The resource may have been already gifted')
    @operation_ns.response(500, description='Internal Server Error')
    def post(self, purchase_uuid):
        data = Customer.decode_auth_token(request.headers['Authorization'])
        return undo_purchase(purchase_uuid, data['customer']), 204


@operation_ns.route('/checkPurchase/<string:purchase_uuid>')
class OperationCheckPurchase(Resource):

    @customer_token_required
    @operation_ns.doc('check_purchase', security='JWT')
    @operation_ns.response(200, 'Check gifter')
    @operation_ns.response(404, 'The resource cannot be found')
    @operation_ns.response(500, 'Something strange happened internally')
    @operation_ns.marshal_with(operation_check_gift_model)
    def get(self, purchase_uuid):
        purchase = Purchase.query.filter_by(purchase_code_uuid=purchase_uuid).first()
        if purchase is None:
            raise NotFound('purchase_uuid not found')
        customer = Customer.query.filter_by(customer_mail_address=purchase.purchase_customer_mail_address).first()
        if customer is None:
            raise InternalServerError('customer not found in db')
        response = {
            'purchase_gifted': purchase.purchase_gifted,
            'purchase_date': purchase.purchase_date,
            'customer_first_name': customer.customer_first_name,
            'customer_last_name': customer.customer_last_name
        }
        return response, 200


